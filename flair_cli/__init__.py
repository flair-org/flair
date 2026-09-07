"""
Flair CLI - versioning Machine Learning models
"""
import sys

if sys.platform == "win32":
    try:
        if sys.stdout and hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        if sys.stderr and hasattr(sys.stderr, "reconfigure"):
            sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    try:
        from rich._win32_console import LegacyWindowsTerm
        _orig_write_text = LegacyWindowsTerm.write_text
        def _safe_write_text(self, text: str) -> None:
            try:
                _orig_write_text(self, text)
            except UnicodeEncodeError:
                clean_text = text.encode("ascii", errors="replace").decode("ascii")
                _orig_write_text(self, clean_text)
        LegacyWindowsTerm.write_text = _safe_write_text
    except Exception:
        pass
