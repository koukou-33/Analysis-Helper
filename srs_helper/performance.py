"""Best-effort local JSON-lines metrics; never serialize prompts, SQL or credentials."""
import ctypes
import json
import os
import time
import warnings
from contextvars import ContextVar
from datetime import datetime, timezone
from functools import wraps
from pathlib import Path
from threading import Event, Thread
from uuid import uuid4

import portalocker

LOG_PATH = Path(__file__).resolve().parent.parent / 'workspace_storage' / 'logs' / 'performance.txt'
_trace = ContextVar('performance_trace', default=None)


def rss_bytes():
    """Current whole-process resident memory, including native allocations."""
    try:
        if os.name == 'nt':
            from ctypes import wintypes
            class Counters(ctypes.Structure):
                _fields_ = [('cb', wintypes.DWORD), ('faults', wintypes.DWORD)] + [
                    (name, ctypes.c_size_t) for name in ('peak_ws','ws','peak_pool','pool','peak_nonpool','nonpool','page','peak_page')]
            counters = Counters()
            counters.cb = ctypes.sizeof(counters)
            kernel = ctypes.WinDLL('kernel32', use_last_error=True)
            kernel.GetCurrentProcess.restype = wintypes.HANDLE
            psapi = ctypes.WinDLL('psapi', use_last_error=True)
            psapi.GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.POINTER(Counters), wintypes.DWORD]
            if psapi.GetProcessMemoryInfo(kernel.GetCurrentProcess(), ctypes.byref(counters), counters.cb):
                return counters.ws
        elif Path('/proc/self/statm').exists():
            return int(Path('/proc/self/statm').read_text().split()[1]) * os.sysconf('SC_PAGE_SIZE')
    except (OSError, ValueError, AttributeError):
        pass
    return None


def write_record(record):
    try:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with portalocker.Lock(str(LOG_PATH), mode='a', encoding='utf-8', timeout=1) as stream:
            stream.write(json.dumps(record, ensure_ascii=False, allow_nan=False) + '\n')
    except Exception:
        # Telemetry must not discard otherwise valid analysis results.
        warnings.warn('Performance log could not be written.', RuntimeWarning, stacklevel=2)


def validation_event(stage, attempt, details):
    write_record({'timestamp_utc':datetime.now(timezone.utc).isoformat(),
                  'trace_id':_trace.get(), 'stage':stage, 'attempt':attempt,
                  'status':'REJECTED', 'diagnostics':details})


def service_event(**metadata):
    """Caller supplies only allowlisted response metadata, never response text."""
    write_record({'timestamp_utc': datetime.now(timezone.utc).isoformat(),
                  'trace_id': _trace.get(), 'stage': 'model_response', **metadata})


def measured(stage):
    def decorate(function):
        @wraps(function)
        def wrapped(*args, **kwargs):
            trace_id = _trace.get() or str(uuid4())
            token = _trace.set(trace_id)
            started = time.perf_counter()
            start_rss = rss_bytes()
            peak = start_rss
            stop = Event()
            def sample():
                nonlocal peak
                while not stop.wait(0.1):
                    value = rss_bytes()
                    if value is not None:
                        peak = max(peak or 0, value)
            worker = Thread(target=sample, daemon=True)
            worker.start()
            record = {'timestamp_utc': datetime.now(timezone.utc).isoformat(), 'trace_id': trace_id,
                      'span_id': str(uuid4()), 'stage': stage, 'pid': os.getpid(), 'status': 'OK'}
            try:
                result = function(*args, **kwargs)
                try:
                    if stage == 'load_dataset':
                        directory, meta = result
                        record.update(dataset_id=meta['dataset_id'], dataset_status=meta['status'],
                                      file_count=len(meta['files']), input_rows=sum(f.get('records',0) for f in meta['files'] if f['status']=='LOADED'),
                                      input_bytes=sum(f.get('size',0) for f in meta['files']),
                                      supplemental_numeric_rows=sum(f.get('supplemental_numeric_rows',0) for f in meta['files'] if f['status']=='LOADED'),
                                      max_supplemental_fields=max((f.get('supplemental_field_count',0) for f in meta['files'] if f['status']=='LOADED'),default=0),
                                      max_input_columns=max((len(f.get('columns',[])) for f in meta['files']), default=0),
                                      database_bytes=(directory/'analysis.duckdb').stat().st_size)
                    elif stage == 'diagnosis' and isinstance(result, dict):
                        record.update({k: result[k] for k in ('dataset_id','session_id','action_id') if k in result})
                        record['analysis_status'] = result.get('status')
                        record['backfill_rounds'] = result.get('backfill_rounds',0)
                        record['goal_count'] = len(result.get('goals',[]))
                        record['verified_goals'] = sum(g.get('status')=='VERIFIED' for g in result.get('goals',[]))
                        record['route'] = result.get('route')
                        loop=result.get('verification_loop',{})
                        record['verification_stop_reason']=loop.get('stop_reason')
                        record['verification_queries']=loop.get('round',0)
                        record['verification_planner_calls']=loop.get('planner_calls',0)
                        record['verification_gap_count']=len(loop.get('missing_information',[]))
                        record['model_status'] = result.get('model_status',{})
                        record['requirement_count'] = len(result.get('requirements',[]))
                        record['covered_requirements'] = sum(r.get('status')=='COVERED' for r in result.get('requirements',[]))
                    elif stage in {'query_plan','manual_sql'}:
                        record.update(artifact_count=len(result), output_rows=sum(a['row_count'] for a in result))
                    elif stage == 'summary':
                        record['scoped_rows'] = result.get('flow_valid_count')
                    elif stage == 'prepare_scope':
                        fields = kwargs.get('required_fields', args[2] if len(args)>2 else ())
                        record['requested_field_count'] = len(fields) if fields is not None else 'ALL'
                except Exception:
                    record['metrics_incomplete'] = True
                return result
            except BaseException as exc:
                record.update(status='ERROR', error_type=type(exc).__name__)
                raise
            finally:
                record['elapsed_seconds'] = round(time.perf_counter()-started,6)
                stop.set()
                worker.join(timeout=1)
                end_rss = rss_bytes()
                if end_rss is not None:
                    peak = max(peak or 0, end_rss)
                record.update(rss_start_bytes=start_rss, rss_end_bytes=end_rss,
                              rss_sampled_peak_bytes=peak,
                              memory_scope='whole_process', sample_interval_ms=100)
                _trace.reset(token)
                write_record(record)
        return wrapped
    return decorate
