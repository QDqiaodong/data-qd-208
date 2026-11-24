import unittest,json
from pathlib import Path
from trace_causality import process
class Regression(unittest.TestCase):
 def test_overlap_union_and_indirect_missing_parent(self):
  d=json.loads(Path('fixtures/input.json').read_text());self.assertEqual(process(d)['traces'][0]['self_time']['root'],'4')
  d['spans'][0]['parent']='db';d['spans'][1]['parent']='missing'
  with self.assertRaises(ValueError):process(d)
