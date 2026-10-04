"""Synthetic contract driver; ordinary use goes through the Hermes CLI/gateway."""
import argparse,json
from .instance import Instance
def main():
 p=argparse.ArgumentParser();p.add_argument('command',choices=['chat','doctor']);p.add_argument('--state',required=True);p.add_argument('--owner',default='local-owner');p.add_argument('--memory',action='store_true');p.add_argument('--life',action='store_true');p.add_argument('--tools',action='store_true');p.add_argument('--text')
 a=p.parse_args();i=Instance(a.state,owner=a.owner,memory=a.memory,life=a.life,tools=a.tools)
 try:
  if a.command=='doctor':print(json.dumps(i.health(),ensure_ascii=False));return
  while True:
   try:text=a.text or input('你: ')
   except (EOFError,KeyboardInterrupt):break
   r=i.chat(text);print(r['reply'],flush=True);i.confirm_visible(r['request_id'])
   if a.text:break
 finally:i.close()
if __name__=='__main__':main()
