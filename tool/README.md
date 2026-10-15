# tool/

`patch_ui.py` and `install_crabe_mod.py` need **`ditool.py`** in this folder. It is a small Python tool
that unpacks and repacks the game's encrypted `.zip` containers (it needs `pip install cryptography`).

`ditool.py` is not included in this repository. Put your copy here as `tool/ditool.py`, set the
environment variable `DITOOL` to its path, or pass `--ditool <path>`. It must provide `unpack(container, out_dir)`, `repack(in_dir, new_container)` and
`verify(container)`.
