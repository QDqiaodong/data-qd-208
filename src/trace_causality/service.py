from .checks import fields,array,ident,number

def process(data):
 fields(data,('spans',));spans=[];ids=set()
 for s in array(data['spans'],1,2000):
  fields(s,('id','trace','parent','service','start','end'));k=ident(s['id']);
  if k in ids:raise ValueError('duplicate span')
  ids.add(k);a=number(s['start']);b=number(s['end']);
  if b<a:raise ValueError('end before start')
  parent=s['parent'] if s['parent'] is None else ident(s['parent']);spans.append({'id':k,'trace':ident(s['trace']),'parent':parent,'service':ident(s['service']),'start':a,'end':b})
 by={s['id']:s for s in spans}
 for s in spans:
  if s['parent'] is not None and (s['parent'] not in by or by[s['parent']]['trace']!=s['trace']):raise ValueError('invalid parent')
 for s in spans:
  seen={s['id']};p=s['parent']
  while p is not None:
   if p in seen:raise ValueError('parent cycle')
   seen.add(p);p=by[p]['parent']
 traces=[]
 for trace in sorted({s['trace'] for s in spans}):
  rows=[s for s in spans if s['trace']==trace];critical=max((s['end'] for s in rows),default=0)-min((s['start'] for s in rows),default=0)
  roots=sorted(s['id'] for s in rows if s['parent'] is None);children={s['id']:[] for s in rows}
  for s in rows:
   if s['parent']:children[s['parent']].append(s['id'])
  exclusive={}
  for s in rows:
   intervals=sorted((max(s['start'],by[k]['start']),min(s['end'],by[k]['end'])) for k in children[s['id']] if max(s['start'],by[k]['start'])<min(s['end'],by[k]['end']))
   covered=0;right=s['start']
   for a,b in intervals:
    covered+=max(0,b-max(a,right));right=max(right,b)
   exclusive[s['id']]=str(s['end']-s['start']-covered)
  traces.append({'trace':trace,'roots':roots,'wall_time':str(critical),'self_time':exclusive,'services':sorted({s['service'] for s in rows}),'children':{k:sorted(v) for k,v in children.items()}})
 return {'traces':traces,'summary':{'spans':len(spans),'traces':len(traces),'roots':sum(len(t['roots']) for t in traces)}}
