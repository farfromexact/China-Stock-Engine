from dataclasses import asdict
from pathlib import Path
from tempfile import TemporaryDirectory
import json
import unittest
from unittest.mock import patch,MagicMock

from china_stock_engine.wind_daily import settings
from china_stock_engine.wind_provider import WindProvider,WindProfile
from china_stock_engine import wind_session,wind_coordination

class ParallelDefaultsTests(unittest.TestCase):
    def test_daily_default_config(self):
        with TemporaryDirectory() as temp:
            root=Path(temp);path=root/'config.json';path.write_text(json.dumps(dict(enabled=False)))
            cfg=settings(path,root)
            self.assertTrue(cfg['allow_parallel_collectors'])
            self.assertTrue(cfg['allow_dashboard'])
            self.assertEqual(cfg['request_interval_seconds'],0)
            self.assertFalse(cfg['enabled'])

    def test_zero_interval_preserves_verified_profile(self):
        profile=WindProfile(units_verified=True,min_interval=2)
        before=asdict(profile)
        provider=WindProvider(lambda *args: {},profile,request_interval=0)
        with patch('china_stock_engine.wind_provider.time.monotonic',return_value=0),patch('china_stock_engine.wind_provider.time.sleep') as sleep:
            provider.request('wsd');provider.request('wsd')
            sleep.assert_not_called()
        self.assertEqual(asdict(profile),before)
        self.assertEqual(provider.provenance['profile'],before)

    def test_explicit_parallel_worker_bypasses_only_cross_engine_lock(self):
        with TemporaryDirectory() as temp,patch.object(wind_coordination,'collection_lock_path',return_value=Path(temp)/'lock'):
            with wind_coordination.collection_lock(),patch.object(wind_session,'_serve') as serve:
                pipe=MagicMock();wind_session._worker(pipe,0,True)
                serve.assert_called_once_with(pipe,0)

    def test_pacer_zero_no_sleep(self):
        with patch.object(wind_coordination.time,'monotonic',return_value=0),patch.object(wind_coordination.time,'sleep') as sleep:
            p=wind_coordination.RequestPacer(0)
            with p.slot(): pass
            with p.slot(): pass
            sleep.assert_not_called()
