import json
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/"scripts"))
import github_workflow_dispatch as g

def policy(tmp_path):
    p=tmp_path/"p.json"; p.write_text(json.dumps({"version":1,"allowed_dispatches":[{"repository":"o/r","workflow":"w.yml","refs":["safe-ref"]}]})); return g.load_policy(p)

def test_allowlisted_dispatch(tmp_path):
    assert g.allowed(policy(tmp_path),"o/r","w.yml","safe-ref")

def test_rejects_wrong_ref(tmp_path):
    assert not g.allowed(policy(tmp_path),"o/r","w.yml","main")

def test_rejects_wrong_workflow(tmp_path):
    assert not g.allowed(policy(tmp_path),"o/r","other.yml","safe-ref")

def test_rejects_wrong_repository(tmp_path):
    assert not g.allowed(policy(tmp_path),"x/y","w.yml","safe-ref")
