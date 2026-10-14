"""Workspace launcher for the resumable AKShare collector."""
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from china_stock_engine.akshare_backfill import main
raise SystemExit(main())
