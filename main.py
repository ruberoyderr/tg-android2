# main.py — Android entry point wrapper for PySide6 app
import asyncio
import app as app_module  # your app.py is the main module

if __name__ == "__main__":
    try:
        asyncio.run(app_module.amain())
    except KeyboardInterrupt:
        pass
