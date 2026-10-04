"""Run the bundled single-database Life Supply owner with explicit env settings."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'components/supply'))
from lifesupply.service.life_supply import ServiceSettings, run_service
sys.exit(run_service(ServiceSettings.from_env()))
