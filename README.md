# arxiv_prep

## main2: Features

- [x] Recursively parse commands
- [x] Find used image files, discard res
- [x] Strip comments but make sure to not delete `%` needed for style (e.g. end of line)
- [x] Compile and keep .bbl file
- [x] Pack all needed files as a .tar
- [x] Keep output PDF to double check
- [ ] Convert images to JPGs

Example command:

```bash
python main2.py /path/to/main.tex --rename my_paper_final
```


