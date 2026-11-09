"""Bounded deterministic coverage examples, never population estimators."""
import hashlib
from .sessions import dumps


class CoveragePreview:
    def __init__(self, columns, max_groups=25):
        self.keys=[k for k in ('logicCellId','ue_uid','state') if k in columns]
        self.groups={}
        self.max_groups=max_groups
        self.columns=columns
        self.count=0
        self.small=[]

    def add(self,row):
        self.count+=1
        if self.count<=100: self.small.append(row)
        elif self.count==101: self.small=[]
        key=dumps([row.get(k) for k in self.keys])
        rank=hashlib.sha256(key.encode()).hexdigest()
        if rank not in self.groups:
            if len(self.groups)>=self.max_groups:
                worst=max(self.groups)
                if rank>worst: return
                del self.groups[worst]
            self.groups[rank]={'first':None,'last':None,'samples':{}}
        group=self.groups[rank]
        identity=dumps([row.get(k) for k in ('record_id','streamId','event_ts','state')]) if row.get('record_id') is not None else dumps(row)
        order=(str(row.get('event_ts') or ''),identity)
        if group['first'] is None or order<group['first'][0]: group['first']=(order,row)
        if group['last'] is None or order>group['last'][0]: group['last']=(order,row)
        digest=hashlib.sha256(identity.encode()).hexdigest()
        group['samples'][digest]=row
        if len(group['samples'])>2: del group['samples'][max(group['samples'])]

    def rows(self):
        if self.count<=100: return self.small
        result=[]
        for _,group in sorted(self.groups.items()):
            unique={dumps(r):r for r in [group['first'][1],*group['samples'].values(),group['last'][1]]}
            result.extend(sorted(unique.values(),key=lambda r:(str(r.get('event_ts') or ''),dumps(r))))
        return result

    def metadata(self,total_groups=None):
        return {'method':'DETERMINISTIC_GROUP_COVERAGE','group_keys':self.keys,
                'selected_groups':len(self.groups),'total_groups':total_groups,
                'max_rows':self.max_groups*4,'per_group':'首末观测和两个稳定哈希样例；不是无偏样本'}
