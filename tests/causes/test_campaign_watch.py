import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from causes import campaign_watch as w


class WatchTests(unittest.TestCase):
    def test_owned_stop_finishes_current_schedule_and_retains_closed_cycle(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); directory = root/'watch'; directory.mkdir()
            first = root/'first.py'
            first.write_text("from pathlib import Path\nPath("+repr(str(directory/'STOP'))+").write_text('owned fixture stop')\n")
            last = root/'last.py'; last.write_text("print('last observation')\n")
            def row(path):
                raw = path.read_bytes()
                return {'script':path.name,'timeout_seconds':3,'source':{'path':str(path),'bytes':len(raw),'sha256':hashlib.sha256(raw).hexdigest()}}
            config = {'schema':'causes.campaign_watch_config.v1','directory':str(directory),
                'script_directory':str(root),'watcher_sha256':hashlib.sha256(Path(w.__file__).read_bytes()).hexdigest(),
                'interval_seconds':120,'checks':[row(first)],'periodic':{'every':5,'check':row(last)}}
            w.run(config)
            results = list(directory.glob('*/RESULT.json'))
            self.assertEqual(len(results),1)
            value = json.loads(results[0].read_bytes())
            self.assertEqual([x['script'] for x in value['checks']],['first.py','last.py'])
            self.assertTrue(all(x['returncode']==0 and x['timeout'] is False for x in value['checks']))
            self.assertTrue((directory/'STOP').exists())
            last.write_text("raise RuntimeError('unreviewed')\n")
            with self.assertRaisesRegex(ValueError,'watcher_check_source_changed'):w.validate(config)

    def test_explicit_timeout_records_only_owned_helper_termination(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); source = root/'slow.py'; source.write_text('import time\ntime.sleep(20)\n')
            iteration = root/'iteration'; iteration.mkdir()
            value = w.execute_check(root,iteration,{'script':source.name,'timeout_seconds':0.01})
            self.assertEqual(value,{'script':'slow.py','returncode':-9,'timeout':True})


if __name__ == '__main__':unittest.main()
