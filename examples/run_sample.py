from trace_causality import process
from trace_causality.io import load,publish
if __name__=='__main__':
 r=process(load('fixtures/input.json'));publish('run/sample.json',r);print(r['summary'])
