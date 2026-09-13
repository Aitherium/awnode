import sys, traceback
try:
    import awnode
    print("SMOKE_IMPORT: OK")
    print("VERSION:", getattr(awnode, "__version__", "<missing>"))
    from awnode.cli import main
    print("CLI_MAIN_CALLABLE:", callable(main))
except Exception as e:
    print("SMOKE_IMPORT: FAIL")
    print(type(e).__name__, e)
    traceback.print_exc()
    sys.exit(1)
