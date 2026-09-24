from __future__ import annotations

# import an additional thing for proper PyInstaller freeze support
from multiprocessing import freeze_support


if __name__ == "__main__":
    freeze_support()
    import io
    import os
    import sys
    import signal
    import asyncio
    import logging
    import argparse
    import warnings
    import traceback
    import tkinter as tk
    from tkinter import messagebox
    from typing import NoReturn, TYPE_CHECKING

    import truststore
    truststore.inject_into_ssl()

    from translate import _
    from twitch import Twitch
    from settings import Settings
    from version import __version__
    from exceptions import CaptchaRequired
    from utils import lock_file, resource_path, set_root_icon
    from constants import (
        LOGGING_LEVELS, SELF_PATH, FILE_FORMATTER, LOG_PATH, LOCK_PATH,
        SanitizingFilter, IS_PACKAGED
    )

    if TYPE_CHECKING:
        from _typeshed import SupportsWrite

    warnings.simplefilter("default", ResourceWarning)

    # import tracemalloc
    # tracemalloc.start(3)

    if sys.version_info < (3, 10):
        raise RuntimeError("Python 3.10 or higher is required")

    # Suppress X11 Input Method registration on Linux to prevent 
    # XWayland/Mutter lockups during heavy Tkinter layout updates.
    if sys.platform.startswith("linux") and "XMODIFIERS" not in os.environ:
        os.environ["XMODIFIERS"] = "@im=none"

    class Parser(argparse.ArgumentParser):
        def __init__(self, *args, **kwargs) -> None:
            super().__init__(*args, **kwargs)
            self._message: io.StringIO = io.StringIO()

        def _print_message(self, message: str, file: SupportsWrite[str] | None = None) -> None:
            self._message.write(message)
            # print(message, file=self._message)

        def exit(self, status: int = 0, message: str | None = None) -> NoReturn:
            try:
                super().exit(status, message)  # sys.exit(2)
            finally:
                messagebox.showerror("Argument Parser Error", self._message.getvalue())

    class ParsedArgs(argparse.Namespace):
        _verbose: int
        _debug_ws: bool
        _debug_gql: bool
        log: bool
        tray: bool
        dump: bool

        # TODO: replace int with union of literal values once typeshed updates
        @property
        def logging_level(self) -> int:
            return LOGGING_LEVELS[min(self._verbose, 4)]

        @property
        def debug_ws(self) -> int:
            """
            If the debug flag is True, return DEBUG.
            If the main logging level is DEBUG, return INFO to avoid seeing raw messages.
            Otherwise, return NOTSET to inherit the global logging level.
            """
            if self._debug_ws:
                return logging.DEBUG
            elif self._verbose >= 4:
                return logging.INFO
            return logging.NOTSET

        @property
        def debug_gql(self) -> int:
            if self._debug_gql:
                return logging.DEBUG
            elif self._verbose >= 4:
                return logging.INFO
            return logging.NOTSET

    if sys.platform == "win32":
        import ctypes
        try:
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("devilxd.twitchdropsminer")
        except Exception:
            pass

    # handle input parameters
    # NOTE: parser output is shown via message box
    # we also need a dummy invisible window for the parser
    root = tk.Tk()
    root.overrideredirect(True)
    root.withdraw()
    set_root_icon(root, resource_path("icons/pickaxe.ico"))
    root.update()
    parser = Parser(
        SELF_PATH.name,
        description="A program that allows you to mine timed drops on Twitch.",
    )
    parser.add_argument("--version", action="version", version=f"v{__version__}")
    parser.add_argument("-v", dest="_verbose", action="count", default=0)
    parser.add_argument("--tray", action="store_true")
    parser.add_argument("--log", action="store_true")
    parser.add_argument("--dump", action="store_true")
    # undocumented debug args
    parser.add_argument(
        "--debug-ws", dest="_debug_ws", action="store_true", help=argparse.SUPPRESS
    )
    parser.add_argument(
        "--debug-gql", dest="_debug_gql", action="store_true", help=argparse.SUPPRESS
    )
    args = parser.parse_args(namespace=ParsedArgs())
    # load settings
    try:
        settings = Settings(args)
    except Exception:
        messagebox.showerror(
            "Settings error",
            f"There was an error while loading the settings file:\n\n{traceback.format_exc()}"
        )
        sys.exit(4)
    # dummy window isn't needed anymore
    root.destroy()
    # get rid of unneeded objects
    del root, parser

    # client run
    async def main():
        # set language
        try:
            _.set_language(settings.language)
        except ValueError:
            # this language doesn't exist - stick to English
            pass

        # Determine effective core logging level:
        # If any debug flag is set (--debug-gql, --debug-ws, -vvvv), enable DEBUG (10).
        if (
            settings.debug_gql == logging.DEBUG
            or settings.debug_ws == logging.DEBUG
            or settings.logging_level <= logging.DEBUG
        ):
            effective_level = logging.DEBUG
        elif settings._verbose == 0:
            effective_level = logging.INFO
        else:
            effective_level = settings.logging_level

        logger = logging.getLogger("TwitchDrops")
        logger.setLevel(effective_level)
        logger.addFilter(SanitizingFilter())

        if settings.log:
            from logging.handlers import RotatingFileHandler
            handler = RotatingFileHandler(
                LOG_PATH, maxBytes=10 * 1024 * 1024, backupCount=3, encoding="utf-8"
            )
            handler.setLevel(settings.logging_level)
            handler.setFormatter(FILE_FORMATTER)
            handler.addFilter(SanitizingFilter())
            logger.addHandler(handler)

        logging.getLogger("TwitchDrops.gql").setLevel(settings.debug_gql)
        logging.getLogger("TwitchDrops.websocket").setLevel(settings.debug_ws)

        import platform
        proxy_info = f"{settings.proxy.host}:{settings.proxy.port}" if settings.proxy.host else "Direct"
        logger.info(
            f"Twitch Drops Miner v{__version__} | Python {platform.python_version()} on {sys.platform} | "
            f"Mode: {settings.priority_mode.name} | Lang: {settings.language} | Proxy: {proxy_info}"
        )

        exit_status = 0
        client = Twitch(settings)
        import updater
        loop = asyncio.get_running_loop()
        if sys.platform == "linux":
            loop.add_signal_handler(signal.SIGINT, lambda *_: client.gui.close())
            loop.add_signal_handler(signal.SIGTERM, lambda *_: client.gui.close())
        update_task = loop.create_task(updater.run_update_loop(client.gui))
        try:
            await client.run()
        except CaptchaRequired:
            exit_status = 1
            client.prevent_close()
            client.print(_("error", "captcha"))
        except Exception:
            exit_status = 1
            client.prevent_close()
            watching = client.watching_channel.get_with_default(None)
            watching_name = watching.name if watching else "None"
            state_name = client._state.name if hasattr(client, "_state") else "Unknown"
            crash_diag = (
                f"Fatal error encountered:\n"
                f"Version: v{__version__} (Packaged: {IS_PACKAGED}) | OS: {sys.platform}\n"
                f"State: {state_name} | Watching: {watching_name}\n"
                f"Last GQL Op: {getattr(client, '_last_gql_op', 'None')}\n\n"
                f"{traceback.format_exc()}"
            )
            logger.error(crash_diag)
        finally:
            if sys.platform == "linux":
                loop.remove_signal_handler(signal.SIGINT)
                loop.remove_signal_handler(signal.SIGTERM)
            if not update_task.done():
                update_task.cancel()
            client.print(_("gui", "status", "exiting"))
            await client.shutdown()
        if not client.gui.close_requested:
            # user didn't request the closure
            client.gui.tray.change_icon("error")
            client.print(_("status", "terminated"))
            client.gui.status.update(_("gui", "status", "terminated"))
            # notify the user about the closure
            client.gui.grab_attention(sound=True)
        await client.gui.wait_until_closed()
        # save the application state
        # NOTE: we have to do it after wait_until_closed,
        # because the user can alter some settings between app termination and closing the window
        client.save(force=True)
        client.gui.stop()
        client.gui.close_window()
        sys.exit(exit_status)

    try:
        # use lock_file to check if we're not already running
        success, file = lock_file(LOCK_PATH)
        if not success:
            # already running - exit
            sys.exit(3)

        asyncio.run(main())
    finally:
        file.close()
