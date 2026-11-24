import json,unittest,copy
from pathlib import Path
from trace_causality import process
class T(unittest.TestCase):
 def setUp(self):self.d=json.loads(Path('fixtures/input.json').read_text())
 def test_tree(self):
  r=process(self.d);self.assertEqual(r['summary'],{'spans':3,'traces':1,'roots':1});self.assertEqual(r['traces'][0]['children']['root'],['cache','db']);self.assertEqual(r['traces'][0]['wall_time'],'10')
 def test_multiple_trace(self):
  self.d['spans'].append({'id':'r2','trace':'t2','parent':None,'service':'worker','start':'1','end':'2'});self.assertEqual(process(self.d)['summary']['traces'],2)
 def test_bad_parent_trace(self):
  self.d['spans'][1]['trace']='t2'
  with self.assertRaises(ValueError):process(self.d)
 def test_cycle(self):
  self.d['spans'][0]['parent']='db'
  with self.assertRaises(ValueError):process(self.d)
 def test_time(self):
  self.d['spans'][1]['end']='1'
  with self.assertRaises(ValueError):process(self.d)
 def test_duplicate(self):
  self.d['spans'].append(self.d['spans'][0])
  with self.assertRaises(ValueError):process(self.d)
