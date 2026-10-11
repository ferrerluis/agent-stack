"""Independent deadline recovery; never launches or generates a response."""
import argparse
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time


def main():
    p=argparse.ArgumentParser()
    p.add_argument('--run',required=True);p.add_argument('--terraform',required=True);p.add_argument('--deadline',type=float,required=True)
    a=p.parse_args();run=Path(a.run)
    from showcase_cleanup import private_read, private_write
    from showcase_policy import require
    from showcase_attempt import ROOT, private_directory
    private_directory(run)
    prepared=private_read(run/'preparation.json')
    require(run.name==prepared['run_id'] and time.time()<a.deadline<=time.time()+14400,'invalid_watchdog_scope')
    # Poll a private record, never sleep through user-facing controller updates.
    while time.time()<a.deadline-900:
        record=run/'showcase-run.json'
        if record.exists():
            value=private_read(record)
            if value.get('stage')=='finished' and value.get('cleanup') in ('verified','not_reserved'):
                private_write(run/'watchdog-result.json',{'status':'disarmed_after_cleanup'});return 0
        time.sleep(min(10,max(0.1,a.deadline-900-time.time())))
    owner=private_read(run/'controller-owner.json')
    # Only the explicitly recorded dedicated launch process group can be killed.
    pid=owner['pid']
    if Path('/proc/'+str(pid)+'/stat').exists():
        fields=Path('/proc/'+str(pid)+'/stat').read_text().split()
        require(fields[21]==owner['start_ticks'] and os.getpgid(pid)==owner['pgid']
                and owner['pgid']==pid,'watchdog_process_scope_changed')
        os.killpg(pid,signal.SIGTERM)
        time.sleep(5)
        if Path('/proc/'+str(pid)+'/stat').exists():
            fields=Path('/proc/'+str(pid)+'/stat').read_text().split()
            if fields[21]==owner['start_ticks']:os.killpg(pid,signal.SIGKILL)
    # Cleanup uses the original state and independent resource/node inventories.
    args=[sys.executable,'-B',str(ROOT/'scripts/showcase_live.py'),'--config',str(run/'inputs.json'),
          '--provider',prepared['scope']['provider'],'--run-id',run.name,'--batch',str(run.parent.parent),
          '--terraform',a.terraform,'--phase','cleanup']
    for _ in range(3):
        result=subprocess.run(args,capture_output=True,timeout=1800,env=dict(os.environ,AGENT_STACK_LIVE_TESTS='1',PYTHONDONTWRITEBYTECODE='1'))
        if result.returncode==0:
            private_write(run/'watchdog-result.json',{'status':'cleanup_verified'});return 0
        time.sleep(10)
    private_write(run/'watchdog-result.json',{'status':'cleanup_unverified'});return 1


if __name__=='__main__':
    try:sys.exit(main())
    except Exception:
        print('{"status":"watchdog_refused_or_failed"}');sys.exit(1)
