"""Effective Linux boundary and exact recovery, including adversarial read-back."""
import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import resolved_tmp
from readtrial import isolation
from readtrial.process import Result
from readtrial.targets import Target, TargetError, Limits
from platforms import posix_only

class BoundaryTests(unittest.TestCase):
    def setUp(self):
        self.target = Target('lxc:cached', Limits(), lambda *a: None, pool='sample-pool')
        self.target.proxy = type('Proxy', (), {'port': 12345})()
        self.config = {'name': self.target.id, 'profiles': [], 'expanded_config': {'user.readtrial.id': self.target.id, 'security.privileged': 'false', 'security.nesting': 'true', 'security.idmap.isolated': 'true', 'limits.cpu.allowance': '100ms/100ms', 'limits.cpu': '1', 'limits.memory': '512MiB', 'limits.processes': '128'}, 'expanded_devices': {'root': {'type': 'disk', 'path': '/', 'pool': 'sample-pool', 'size': '3072MiB'}, 'registry-proxy': {'type': 'proxy', 'bind': 'container', 'listen': 'tcp:' + '.'.join(map(str, (127, 0, 0, 1))) + ':18080', 'connect': 'tcp:' + '.'.join(map(str, (127, 0, 0, 1))) + ':12345'}}}
    def test_effective_config_attests_exact_boundary(self):
        with patch.object(self.target, 'management', return_value=Result(0, json.dumps(self.config), 0)):
            self.target.attest_lxc_config()
        self.assertEqual(self.target.capabilities['effective']['identity']['status'], 'config-attested')
    def test_unsafe_or_missing_readback_refused(self):
        bad = []
        for field, value in [('profiles', ['default']), ('name', 'other')]:
            d=copy.deepcopy(self.config);d[field]=value;bad.append(d)
        for key, value in [('security.privileged','true'),('limits.memory',''),('limits.cpu','2'),('limits.processes',''),('raw.lxc','lxc.mount.entry=/host'),('security.idmap.isolated','false')]:
            d=copy.deepcopy(self.config);d['expanded_config'][key]=value;bad.append(d)
        for device in [{'type':'nic'}, {'type':'disk','source':'/host','path':'/host'}, {'type':'unix-char','source':'/dev/test'}]:
            d=copy.deepcopy(self.config);d['expanded_devices']['extra']=device;bad.append(d)
        d=copy.deepcopy(self.config);d['expanded_devices']['registry-proxy']['connect']='tcp:' + '.'.join(['0'] * 4) + ':12345';bad.append(d)
        for d in bad:
            with self.subTest(d=d), patch.object(self.target, 'management', return_value=Result(0,json.dumps(d),0)):
                with self.assertRaises(TargetError): self.target.attest_lxc_config()
    def test_kernel_limits_must_be_observed_not_echoed(self):
        good={'uid_map': [[0,1000000,65536]], 'memory': '536870912', 'pids':'128','cpu':'100000 100000','interfaces':['lo'],'routes':[]}
        with patch.object(self.target,'management',return_value=Result(0,json.dumps(good),0)):
            self.target.attest_lxc_kernel()
        self.assertEqual(self.target.capabilities['effective']['resources']['status'],'kernel-attested')
        for key,value in [('memory','max'),('pids','max'),('cpu','max 100000'),('interfaces',['lo','eth0']),('routes',['default']),('uid_map',[[0,0,2**32-1]])]:
            d={**good,key:value}
            with self.subTest(key=key),patch.object(self.target,'management',return_value=Result(0,json.dumps(d),0)):
                with self.assertRaises(TargetError):self.target.attest_lxc_kernel()
    @posix_only
    def test_recovery_exact_tag_required_and_idempotent(self):
        with tempfile.TemporaryDirectory() as temp:
            directory=Path(temp)/'journals'; j=isolation.Journal(directory,'readtrial-'+'a'*32,'lxc');j.intent('lxc',j.id);j.intent('target-path','/tmp/'+j.id+'/home')
            before=j.path.read_bytes()
            matching=[{'name':j.id,'config':{'user.readtrial.id':j.id}}]
            with patch('readtrial.isolation.execute',side_effect=[Result(0,json.dumps(matching),0),Result(0,json.dumps(matching),0),Result(0,'',0),Result(0,'[]',0)] ) as run:
                result=isolation.recover(directory,yes=True)
            self.assertTrue(all(x['status']=='absent' for x in result))
            self.assertIn(['lxc','delete','--force',j.id],[c.args[0] for c in run.call_args_list])
            self.assertEqual(j.path.read_bytes(),before)
            with patch('readtrial.isolation.execute',return_value=Result(0,'[]',0)) as run:
                isolation.recover(directory,yes=True)
            self.assertFalse(any('delete' in c.args[0] for c in run.call_args_list))
            with patch('readtrial.isolation.execute',return_value=Result(0,json.dumps([{'name':j.id,'config':{}}]),0)) as run:
                with self.assertRaises(ValueError):isolation.recover(directory,yes=True)
            self.assertFalse(any('delete' in c.args[0] for c in run.call_args_list))
    @posix_only
    def test_recovery_no_yes_and_unknown_resources_never_delete(self):
        with tempfile.TemporaryDirectory() as temp:
            directory=Path(temp)/'journals';j=isolation.Journal(directory,'readtrial-'+'b'*32,'ssh');j.intent('target-path','/tmp/'+j.id)
            with patch('readtrial.isolation.execute') as run:
                with self.assertRaises(ValueError):isolation.recover(directory,yes=False)
                with self.assertRaises(ValueError):isolation.recover(directory,yes=True)
            run.assert_not_called()

    def test_repository_commands_refused_without_attestation(self):
        with self.assertRaisesRegex(TargetError, 'unproven'):
            self.target.command('echo never-run', setup=True)

    def test_namespace_readback_enforces_workspace_and_process_limits(self):
        good={'workspace_bytes':1024*1024*1024,'root_readonly':True,'usr_readonly':True,'as':512*1024*1024,'fsize':1024*1024*1024,'cpu':300}
        with patch.object(self.target,'management',return_value=Result(0,json.dumps(good),0)):
            self.target.attest_namespace(['bootstrap'],'bootstrap')
        for key,value in [('workspace_bytes',0),('workspace_bytes',2**40),('root_readonly',False),('usr_readonly',False),('as',-1),('fsize',-1),('cpu',-1)]:
            with self.subTest(key=key),patch.object(self.target,'management',return_value=Result(0,json.dumps({**good,key:value}),0)):
                with self.assertRaises(TargetError):self.target.attest_namespace(['bootstrap'],'bootstrap')

    @posix_only
    def test_recovery_rechecks_tag_after_discovery_before_delete(self):
        with tempfile.TemporaryDirectory() as temp:
            directory=Path(temp)/'journals';j=isolation.Journal(directory,'readtrial-'+'c'*32,'lxc');j.intent('lxc',j.id)
            matching=[{'name':j.id,'config':{'user.readtrial.id':j.id}}]
            swapped=[{'name':j.id,'config':{'user.readtrial.id':'other'}}]
            with patch('readtrial.isolation.execute',side_effect=[Result(0,json.dumps(matching),0),Result(0,json.dumps(swapped),0)]) as run:
                with self.assertRaisesRegex(ValueError,'changed'):isolation.recover(directory,yes=True)
            self.assertFalse(any('delete' in c.args[0] for c in run.call_args_list))

    @posix_only
    def test_recovery_covers_each_partial_creation_phase(self):
        for phase in ('creating','running','closing','incomplete'):
            with self.subTest(phase=phase),tempfile.TemporaryDirectory() as temp:
                directory=Path(temp)/'journals';j=isolation.Journal(directory,'readtrial-'+'d'*32,'lxc');j.intent('lxc',j.id);j.phase(phase)
                with patch('readtrial.isolation.execute',return_value=Result(0,'[]',0)) as run:
                    self.assertEqual(isolation.recover(directory,yes=True)[0]['status'],'absent')
                self.assertFalse(any('delete' in c.args[0] for c in run.call_args_list))

    @posix_only
    def test_root_quota_requires_bounded_independent_exercise(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);self.target.configure_isolation(root/'out',{},root/'bundle')
            pool={'space':{'total':30*1024**3,'used':2*1024**3}}
            proof={'errno':122,'bytes_written':900*1024**2,'probe_file_absent':True}
            with patch.object(self.target,'management',side_effect=[Result(0,json.dumps(pool),0),Result(0,json.dumps(proof),0),Result(0,json.dumps(pool),0)]):
                self.target.attest_root_quota()
            self.assertTrue(self.target.capabilities['effective']['root_disk']['independent_enforcement_proof'])
            for bad in ({**proof,'errno':0},{**proof,'errno':28},{**proof,'probe_file_absent':False},{**proof,'bytes_written':2**40}):
                with patch.object(self.target,'management',side_effect=[Result(0,json.dumps(pool),0),Result(0,json.dumps(bad),0),Result(0,json.dumps(pool),0)]):
                    with self.assertRaises(TargetError):self.target.attest_root_quota()
            small={'space':{'total':4*1024**3,'used':3*1024**3}}
            with patch.object(self.target,'management',return_value=Result(0,json.dumps(small),0)) as run:
                with self.assertRaises(TargetError):self.target.attest_root_quota()
            self.assertEqual(run.call_count,1)

    def test_no_repository_command_with_only_driver_capability(self):
        self.target.capabilities['effective']['identity']['status']='kernel-attested'
        self.target.capabilities['effective']['resources']['status']='kernel-attested'
        self.target.capabilities['effective']['filesystem']['status']='namespace-attested'
        self.target.capabilities['effective']['root_disk']={'status':'driver-capable','independent_enforcement_proof':False}
        with self.assertRaisesRegex(TargetError,'unproven'):
            self.target.command('echo never-run')
