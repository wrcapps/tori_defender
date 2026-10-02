#!/usr/bin/env python3
"""Pull short recorded segments out of the site NVR over ISAPI.

A trimmed COPY of the archive code in the research tree's fetch_pair_window.py (this app is a
standalone deliverable and must not import from that tree). It WILL drift from the original:
if an archive quirk is fixed in one, check the other.

WHY THIS EXISTS
    When a camera cannot be watched live (the WireGuard tunnel is saturated, the camera is
    unreachable, the stream will not decode), the NVR has usually been recording it all along
    on the fast side of the tunnel. acquisition_service.py pulls short segments of that
    recording in the background and runs detection on whatever has arrived.

THE TRAPS THIS CODE ALREADY ENCODES  (each measured on this NVR, DS-7732NXI-I4/16P/S V5.04.070,
each returns plausible WRONG footage rather than an error)
    - Times are DEVICE WALL CLOCK. The trailing 'Z' is not UTC: 03:00:00Z meant 03:00 local.
      Send the site's wall-clock digits and convert nothing.
    - The playbackURI from a search carries name= and size=, which OVERRIDE the requested time
      range (a 1-minute request returned the whole 26-minute, 1 GB segment). Strip both.
    - searchID must be a braced GUID, or the NVR answers HTTP 400 "two root tags".
    - A search reply is capped at 64 matches whatever maxResults says.
    - Segments are ~26 min and differ per channel, so a request is clipped to the segment(s) it
      falls in. A clip also starts on a keyframe, so its first frame can be up to ~1 s away from
      the requested start: frame times derived from a clip carry that uncertainty.
    - The NVR's clock is manual (no NTP) and can differ from this host's, so "now" is read
      from the NVR itself.
"""
from __future__ import annotations

import hashlib
import re
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

NAMESPACE = "http://www.isapi.org/ver20/XMLSchema"
SEARCH_TIMEOUT = 30.0
DOWNLOAD_TIMEOUT = 120.0


class NvrError(RuntimeError):
    pass


def escape_xml(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def find_all(xml: bytes, tag: str) -> list[str]:
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        return []
    return [e.text.strip() for e in root.iter()
            if e.tag.rsplit("}", 1)[-1] == tag and e.text]


def find_first(xml: bytes, tag: str) -> str | None:
    values = find_all(xml, tag)
    return values[0] if values else None


class Nvr:
    """Digest ISAPI against the recorder (HTTPS, self-signed certificate)."""

    def __init__(self, host: str, user: str, password: str, scheme: str = "https",
                 tz: str = "Europe/Bucharest"):
        self.host, self.scheme, self.tz = host, scheme, ZoneInfo(tz)
        context = ssl.create_default_context()
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        manager = urllib.request.HTTPPasswordMgrWithDefaultRealm()
        manager.add_password(None, f"{scheme}://{host}/", user, password)
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPSHandler(context=context),
            urllib.request.HTTPDigestAuthHandler(manager),
            urllib.request.HTTPBasicAuthHandler(manager))
        self.clock_offset = 0.0            # NVR wall clock minus this host's, seconds

    def _request(self, path: str, data: bytes | None, method: str):
        headers = {"Content-Type": "application/xml"} if data else {}
        return urllib.request.Request(f"{self.scheme}://{self.host}{path}", data=data,
                                      method=method, headers=headers)

    def _open(self, request, label: str, timeout: float) -> bytes:
        try:
            with self.opener.open(request, timeout=timeout) as response:
                return response.read()
        except urllib.error.HTTPError as exc:
            try:
                body = exc.read()
            except OSError:
                body = b""
            detail = " / ".join(v for v in (find_first(body, "statusString"),
                                            find_first(body, "subStatusString")) if v)
            raise NvrError(f"{label}: HTTP {exc.code}{': ' + detail if detail else ''}") from exc
        except (urllib.error.URLError, OSError) as exc:
            raise NvrError(f"{label}: {type(exc).__name__}: {exc}") from exc

    def get(self, path: str, timeout: float = SEARCH_TIMEOUT) -> bytes:
        return self._open(self._request(path, None, "GET"), f"GET {path}", timeout)

    def post(self, path: str, xml: bytes, timeout: float = SEARCH_TIMEOUT) -> bytes:
        return self._open(self._request(path, xml, "POST"), f"POST {path}", timeout)

    def download_to(self, playback_uri: str, dest: Path, timeout: float = DOWNLOAD_TIMEOUT) -> int:
        """Stream one playback URI into dest; written as .part and renamed on success."""
        body = (f'<?xml version="1.0" encoding="utf-8"?>'
                f'<downloadRequest version="1.0" xmlns="{NAMESPACE}">'
                f'<playbackURI>{escape_xml(playback_uri)}</playbackURI></downloadRequest>').encode()
        partial = dest.with_suffix(dest.suffix + ".part")
        total = 0
        try:
            with self.opener.open(self._request("/ISAPI/ContentMgmt/download", body, "POST"),
                                  timeout=timeout) as response, partial.open("wb") as handle:
                while True:
                    block = response.read(64 * 1024)
                    if not block:
                        break
                    handle.write(block)
                    total += len(block)
        except (urllib.error.URLError, OSError) as exc:
            partial.unlink(missing_ok=True)
            raise NvrError(f"download {dest.name}: {type(exc).__name__}: {exc}") from exc
        if total == 0:
            partial.unlink(missing_ok=True)
            raise NvrError(f"download {dest.name}: empty body")
        partial.replace(dest)
        return total

    # ------------------------------------------------------------------ time
    def sync_clock(self) -> float:
        """Measure how far the NVR's wall clock is from this host's. Returns the offset."""
        body = self.get("/ISAPI/System/time", timeout=10.0)
        text = find_first(body, "localTime") or ""
        wall = datetime.strptime(text[:19], "%Y-%m-%dT%H:%M:%S")
        local = datetime.now(self.tz).replace(tzinfo=None)
        self.clock_offset = (wall - local).total_seconds()
        return self.clock_offset

    def now(self) -> datetime:
        """The NVR's current wall clock, as an aware datetime in the site timezone."""
        return (datetime.now(self.tz) + timedelta(seconds=self.clock_offset))

    def extended(self, moment: datetime) -> str:
        return moment.astimezone(self.tz).strftime("%Y-%m-%dT%H:%M:%SZ")   # the Z is a fiction

    def basic(self, moment: datetime) -> str:
        return moment.astimezone(self.tz).strftime("%Y%m%dT%H%M%SZ")

    def parse(self, text: str | None) -> datetime | None:
        for pattern in ("%Y-%m-%dT%H:%M:%SZ", "%Y%m%dT%H%M%SZ"):
            try:
                return datetime.strptime(text or "", pattern).replace(tzinfo=self.tz)
            except ValueError:
                continue
        return None

    # --------------------------------------------------------------- channels
    def channel_map(self) -> dict[str, int]:
        """Camera address -> NVR channel, asked of the NVR (the configs disagree with each other)."""
        root = ET.fromstring(self.get("/ISAPI/ContentMgmt/InputProxy/channels/status", timeout=60))
        found: dict[str, int] = {}
        for channel in root:
            ident = address = online = None
            for element in channel.iter():
                name = element.tag.rsplit("}", 1)[-1]
                if name == "id" and ident is None:
                    ident = (element.text or "").strip()
                elif name == "ipAddress":
                    address = (element.text or "").strip()
                elif name == "online":
                    online = (element.text or "").strip()
            if ident and address and online == "true":
                found[address] = int(ident)
        return found

    # ----------------------------------------------------------------- search
    def search(self, track: int, start: datetime, end: datetime) -> list[dict]:
        """Recorded segments covering [start, end] on a track, oldest first."""
        digest = hashlib.md5(f"{track}-{self.basic(start)}".encode()).hexdigest()
        guid = f"{{{digest[:8]}-{digest[8:12]}-{digest[12:16]}-{digest[16:20]}-{digest[20:32]}}}"
        body = (f'<?xml version="1.0" encoding="utf-8"?>'
                f'<CMSearchDescription version="2.0" xmlns="{NAMESPACE}">'
                f'<searchID>{guid}</searchID>'
                f'<trackIDList><trackID>{track}</trackID></trackIDList>'
                f'<timeSpanList><timeSpan><startTime>{self.extended(start)}</startTime>'
                f'<endTime>{self.extended(end)}</endTime></timeSpan></timeSpanList>'
                f'<maxResults>64</maxResults><searchResultPostion>0</searchResultPostion>'
                f'<metadataList><metadataDescriptor>//recordType.meta.std-cgi.com'
                f'</metadataDescriptor></metadataList></CMSearchDescription>').encode()
        xml = self.post("/ISAPI/ContentMgmt/search", body)
        try:
            root = ET.fromstring(xml)
        except ET.ParseError as exc:
            raise NvrError(f"search on track {track}: unparseable reply") from exc
        out = []
        for item in root.iter():
            if item.tag.rsplit("}", 1)[-1] != "searchMatchItem":
                continue
            blob = ET.tostring(item)
            uris, starts, ends = (find_all(blob, t) for t in ("playbackURI", "startTime", "endTime"))
            if uris and starts and ends and re.search(r"(?i)starttime=", uris[0]):   # live URIs have none
                s, e = self.parse(starts[0]), self.parse(ends[0])
                if s and e:
                    out.append({"uri": uris[0], "start": s, "end": e})
        out.sort(key=lambda seg: seg["start"])
        if not out:
            raise NvrError(f"nothing recorded on track {track} in that span")
        return out

    def retime(self, uri: str, start: datetime, end: datetime) -> str:
        """Trim a segment URI to [start, end]; strips name=/size= (they override the range)."""
        uri = re.sub(r"(?i)&?\b(name|size)=[^&]*", "", uri)
        uri = re.sub(r"\?&+", "?", uri).rstrip("?&")

        def swap(match: re.Match) -> str:
            key, value = match.group(1), match.group(2)
            moment = start if key.lower() == "starttime" else end
            return f"{key}={self.basic(moment) if '-' not in value else self.extended(moment)}"

        out, count = re.subn(r"(?i)\b(starttime|endtime)=([0-9TZ:\-]+)", swap, uri)
        if count < 2:
            out += ("&" if "?" in out else "?") + f"starttime={self.basic(start)}&endtime={self.basic(end)}"
        return out

    def slices(self, start: datetime, end: datetime, segments: list[dict]):
        """[(piece_start, piece_end, uri)] clipped to the segments, plus uncovered seconds."""
        pieces, covered = [], 0.0
        for seg in segments:
            a, b = max(start, seg["start"]), min(end, seg["end"])
            if b > a:
                pieces.append((a, b, self.retime(seg["uri"], a, b)))
                covered += (b - a).total_seconds()
        return pieces, round((end - start).total_seconds() - covered, 1)
