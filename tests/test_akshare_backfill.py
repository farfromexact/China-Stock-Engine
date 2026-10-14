from __future__ import annotations
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import pandas as pd
from china_stock_engine.akshare_backfill import normalize_pair, symbol, thscode, publish
from china_stock_engine.data_reference import apply_adjustments, load_market_history
from china_stock_engine.storage import atomic_write_json, atomic_write_parquet, sha256_file
from china_stock_engine.technical_signals import add_technical_metrics, scan_technical_signals, TechnicalConfig


def pair(n=130):
    dates = pd.bdate_range('2025-01-02', periods=n).strftime('%Y-%m-%d').tolist()
    raw = pd.DataFrame({'trade_date': dates, 'open':9.9, 'high':10., 'low':9.6, 'close':9.9,
                        'volume':1e6, 'amount':150e6, 'turnover_ratio':1.})
    raw.loc[raw.index[-1], ['open','high','low','close','amount']] = [10.,10.35,9.95,10.3,300e6]
    raw['volume'] = raw['amount'] / raw['close']
    adjusted = raw.copy()
    for key in ('open','high','low','close'):
        adjusted[key] = raw[key] * 2 + 5  # Cash-dividend affine offset.
    return raw, adjusted, dates


class AKShareBackfillTests(unittest.TestCase):
    def test_native_adjusted_ohlc_and_inverse_breakout_level(self):
        raw, native, dates = pair()
        observed = dates[-1]+'T20:00:00+08:00'
        normalized = normalize_pair(raw, native, '600000.SH', dates, observed)
        self.assertEqual(len(normalized), 130)
        self.assertNotEqual(float(normalized.iloc[-1]['adj_offset']), 0)
        raw['thscode'] = '600000.SH'
        raw['history_verified'] = True
        output = apply_adjustments(raw, normalized, observed)
        for field in ('open','high','low','close'):
            self.assertAlmostEqual(float(output.iloc[-1]['adjusted_'+field]), float(normalized.iloc[-1]['provider_adj_'+field]))
        metrics = add_technical_metrics(output)
        current = metrics.tail(1).copy()
        current['is_st'],current['is_suspended'],current['is_delisting']=False,False,False
        ref=pd.DataFrame([{'thscode':'600000.SH','as_of_date':dates[-1], 'listing_date':dates[0], 'security_name':'Example'}])
        scan, _ = scan_technical_signals(current, ref, dates, dates[-1], TechnicalConfig())
        self.assertEqual(scan.iloc[0]['signal'], 'BREAKOUT')
        self.assertAlmostEqual(scan.iloc[0]['breakout_level_raw'], 10.)
        self.assertAlmostEqual(scan.iloc[0]['breakout_distance_atr'], .75)

    def test_date_and_adjustment_schema_mismatch_rejected(self):
        raw, native, dates = pair()
        with self.assertRaisesRegex(ValueError, 'date mismatch'):
            normalize_pair(raw, native.iloc[:-1], '600000.SH', dates, dates[-1]+'T20:00:00+08:00')
        native.loc[native.index[-1], 'open'] += 5
        with self.assertRaisesRegex(ValueError, 'inconsistent adjusted OHLC'):
            normalize_pair(raw, native, '600000.SH', dates, dates[-1]+'T20:00:00+08:00')

    def test_today_preclose_uses_timestamped_native_quote(self):
        raw, native, dates = pair()
        qt=['']*31
        qt[4]='10.10'; qt[30]=dates[-1].replace('-','')+'150000'
        frame=normalize_pair(raw,native,'600000.SH',dates,dates[-1]+'T20:00:00+08:00',qt)
        self.assertEqual(frame.iloc[-1]['pre_close'],10.1)
        self.assertEqual(frame.iloc[-1]['pre_close_basis'],'Tencent_current_quote_previous_close')
        self.assertEqual(frame.iloc[0]['pre_close_basis'],'previous_observed_raw_close')

    def test_history_vintage_is_available_only_after_actual_observation(self):
        raw, _, dates = pair(2)
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)
            day=dates[-1]
            for category, close, observed in [('market',10.,day+'T18:00:00+08:00'),('akshare/market_history/vintage=test',11.,day+'T21:00:00+08:00')]:
                path=root/'facts'/category/f'trade_date={day}'/'daily_quotes.parquet'
                atomic_write_parquet(path,raw.tail(1).assign(thscode='600000.SH', close=close))
                atomic_write_json(path.parent/'market_receipt.json',{'schema_version':3,'verified':True,
                    'collection_completed_at':observed,'artifacts':{path.name:{'sha256':sha256_file(path)}}})
            early=load_market_history(root,day,research_asof=day+'T20:00:00+08:00')
            late=load_market_history(root,day,research_asof=day+'T22:00:00+08:00')
            self.assertEqual(early.iloc[0]['close'],10.)
            self.assertEqual(late.iloc[0]['close'],11.)
            # The original fact is retained for historical decisions.
            self.assertEqual(pd.read_parquet(root/'facts/market'/f'trade_date={day}'/'daily_quotes.parquet').iloc[0]['close'],10.)

    def test_partial_collection_never_promotes(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)
            cache=root/'cache'
            atomic_write_parquet(cache/'universe.parquet',pd.DataFrame([{'thscode':'600000.SH','security_name':'Example',
                'security_name_in_time':'Example','as_of_date':'2026-10-12'}]))
            result={'bootstrap':{},'securities':1,'failures':[{'thscode':'600000.SH'}]}
            with self.assertRaisesRegex(ValueError,'incomplete'):
                publish(root/'data',cache,result)
            self.assertFalse((root/'data/latest').exists())

    def test_derived_failure_preserves_previous_latest(self):
        from types import SimpleNamespace
        from china_stock_engine.storage import json_sha256
        raw, adjusted, dates = pair(61)
        observed=dates[-1]+'T23:00:00+08:00'
        panel=normalize_pair(raw,adjusted,'600000.SH',dates,observed)
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp); cache=root/'cache'; data=root/'data'
            universe=pd.DataFrame([{'thscode':'600000.SH','security_name':'Example',
                'security_name_in_time':'Example','as_of_date':dates[-1]}])
            reference=universe.assign(listing_date=dates[0], total_shares=1e9,float_a_shares=8e8,
                source_provider='synthetic',source_endpoint='test',listing_date_basis='test',share_count_reference_date=dates[-1])
            calendar=pd.DataFrame({'as_of_date':dates[-1],'trade_date':dates,'calendar':'SSE','market_code':'212001',
                'is_open':True,'source_provider':'synthetic','source_endpoint':'test'})
            meta={'sessions':dates,'akshare_version':'synthetic','collection_completed_at':observed,
                  'universe_basis':'synthetic_current_universe'}
            for name, frame in [('universe.parquet',universe),('reference.parquet',reference),('calendar.parquet',calendar)]:
                atomic_write_parquet(cache/name,frame)
            path=cache/'securities/600000.SH.parquet'
            atomic_write_parquet(path,panel)
            atomic_write_json(path.with_suffix('.json'),{'bootstrap_sha256':json_sha256(meta),'sha256':sha256_file(path)})
            previous=data/'latest/manifest.json'
            atomic_write_json(previous,{'schema_version':3,'trade_date':dates[-2],'verified':True})
            before=previous.read_bytes()
            quality=SimpleNamespace(ok=True,as_dict=lambda:{'ok':True,'metrics':{}})
            with patch('china_stock_engine.quality.validate_data',return_value=quality), patch(
                    'china_stock_engine.data_reference.build_data_reference_outputs',side_effect=ValueError('derived_contract_failed')):
                with self.assertRaisesRegex(ValueError,'derived_contract_failed'):
                    publish(data,cache,{'bootstrap':meta,'securities':1,'failures':[]})
            self.assertEqual(previous.read_bytes(),before)

    def test_unit_mismatch_is_rejected(self):
        raw, native, dates = pair()
        raw['volume'] *= 100
        with self.assertRaisesRegex(ValueError,'unit mismatch'):
            normalize_pair(raw,native,'689009.SH',dates,dates[-1]+'T20:00:00+08:00')

    def test_complete_three_market_publication_passes_real_schema_validation(self):
        from china_stock_engine.storage import json_sha256, load_manifest
        from china_stock_engine.quality import validate_data as real_validate
        raw, adjusted, dates = pair(61)
        observed=dates[-1]+'T23:00:00+08:00'
        codes=['600000.SH','000001.SZ','920001.BJ']
        def small_universe(*args, **kwargs):
            kwargs['min_universe_size']=3
            return real_validate(*args,**kwargs)
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp); cache=root/'cache'; data=root/'data'
            universe=pd.DataFrame({'thscode':codes,'security_name':['Example']*3,
                'security_name_in_time':['Example']*3,'as_of_date':dates[-1]})
            reference=universe.assign(listing_date='1990-01-01',total_shares=1e9,float_a_shares=8e8,
                source_provider='synthetic',source_endpoint='test',listing_date_basis='test',share_count_reference_date=dates[-1])
            calendar=pd.DataFrame({'as_of_date':dates[-1],'trade_date':dates,'calendar':'SSE','market_code':'212001',
                'is_open':True,'source_provider':'synthetic','source_endpoint':'test'})
            meta={'sessions':dates,'akshare_version':'synthetic','collection_completed_at':observed,
                  'universe_basis':'synthetic_current_universe'}
            for name, frame in [('universe.parquet',universe),('reference.parquet',reference),('calendar.parquet',calendar)]:
                atomic_write_parquet(cache/name,frame)
            for code in codes:
                panel=normalize_pair(raw,adjusted,code,dates,observed)
                path=cache/'securities'/f'{code}.parquet'
                atomic_write_parquet(path,panel)
                atomic_write_json(path.with_suffix('.json'),{'bootstrap_sha256':json_sha256(meta),'sha256':sha256_file(path)})
            with patch('china_stock_engine.quality.validate_data',side_effect=small_universe), patch(
                    'china_stock_engine.pipeline.validate_data',side_effect=small_universe):
                result=publish(data,cache,{'bootstrap':meta,'securities':3,'failures':[]})
            self.assertTrue(result['ok'],result['validation'])
            manifest=load_manifest(data/'latest/manifest.json')
            for name in ['universe','security_reference','quotes','trading_calendar','daily_status']:
                self.assertIn(name+'_schema_signature',manifest['provenance'])
            self.assertEqual(result['technical_signals']['scanned_count'],3)
            self.assertTrue((data/'latest/technical_scan.parquet').exists())

    def test_cli_dispatch_and_market_identity(self):
        from china_stock_engine.cli import main
        with patch('china_stock_engine.akshare_backfill.main',return_value=0) as run:
            self.assertEqual(main(['backfill-akshare','--end','2026-10-12','--collect-only']),0)
            self.assertIn('--sessions',run.call_args.args[0])
            self.assertNotIn('--publish',run.call_args.args[0])
        self.assertEqual(symbol('920001.BJ'),'bj920001')
        self.assertEqual(thscode('688001'),'688001.SH')
        self.assertEqual(thscode('000001'),'000001.SZ')


if __name__ == '__main__':
    unittest.main()
