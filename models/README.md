# models

Put the model files here. The app shows them in **Settings** and in the first-time setup.

- A file ending in **`.pt`** is a YOLO model.
- A file ending in **`.pth`** is an RF-DETR model.

Just copy the file into this folder, then reload the app's Settings page. That is all.

Model files are big, so they are not kept in git.

## Optional: a nicer name

Next to a model, you can add a small text file with the same name and the ending `.yaml`.
For example, for `birds-2026.pth` create `birds-2026.yaml`:

```yaml
label: Birds 2026 (Babadag)
variant: nano
```

- `label` is the name shown in the app.
- `variant` is only for `.pth` files: `nano`, `small`, `medium`, `base` or `large`.
  If you do not know it, leave it out (the app assumes `nano`). If the model does not start, ask who gave you the file which size it is.
