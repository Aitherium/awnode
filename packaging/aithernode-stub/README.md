<!-- Every "aithernode" below is deliberate and must survive any rename sweep:
     this is the redirect package for the OLD name. -->
# aithernode

**This package was renamed to [`awnode`](https://pypi.org/project/awnode/).**

`aithernode` is now a thin alias that installs `awnode`. It exists so existing
installs and lockfiles keep resolving, and it will not be removed.

**The command and import names did change.** Unlike a pure distribution rename,
you need to update calls:

| before | now |
|---|---|
| `aithernode` | `awnode` |
| `aithernode-mcp` | `awnode-mcp` |
| `import aithernode` | `import awnode` |

New installs should use the new name directly:

```bash
pip install awnode
```
