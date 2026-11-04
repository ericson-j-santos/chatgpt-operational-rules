"""Native DB CI preconditions. These static tests do not claim live PostgreSQL E2E."""
from pathlib import Path
import unittest

SOURCE = Path(__file__).resolve().parents[1]/'.github/workflows/validate-rules.yml'
STEP = 'Start and verify native PostgreSQL 16 without containers'


def verify(src: str) -> bool:
    required = (
        'runs-on: ubuntu-24.04',
        'name: '+STEP,
        'sudo systemctl start postgresql.service',
        '/usr/lib/postgresql/16/bin/postgres',
        'SHOW server_version_num',
        "sudo -u postgres createdb --owner=postgres todo_reservation_test",
        'NATIVE_POSTGRES_VERSION_MISMATCH',
        'NATIVE_POSTGRES_IDENTITY_MISMATCH',
        "PGPASSWORD='disposable-ci-only' psql -X -h 127.0.0.1",
        'Run shared triage reservation E2E with real PostgreSQL',
        'TRIAGE_TEST_DSN: host=127.0.0.1 port=5432 dbname=todo_reservation_test',
        'python scripts/triage_reservation_e2e.py',
        'python -m unittest -v tests/test_feature_pr_admission.py tests/test_native_postgres_ci_contract.py',
    )
    if any(v not in src for v in required):
        return False
    if src.count('name: '+STEP)!=1:
        return False
    if 'job.services.' in src or 'image: postgres:' in src:
        return False
    if '\n    services:\n' in src or '\n    container:\n' in src:
        return False
    return src.index('name: '+STEP) < src.index('name: Run shared triage reservation E2E')


class NativePostgresCiContracts(unittest.TestCase):
    def setUp(self):
        self.text=SOURCE.read_text(encoding='utf-8')
    def test_native_preconditions_are_present(self):
        self.assertTrue(verify(self.text))
    def test_docker_service_reintroduced_is_rejected(self):
        self.assertFalse(verify(self.text+'\n    services:\n      triage-postgres:\n        image: postgres:16\n'))
    def test_no_postgres_identity_verification_is_rejected(self):
        self.assertFalse(verify(self.text.replace('NATIVE_POSTGRES_IDENTITY_MISMATCH', 'IDENTITY_BYPASSED')))
    def test_old_service_port_reference_is_rejected(self):
        self.assertFalse(verify(self.text.replace('port=5432 dbname=', 'port=${{ job.services.triage-postgres.ports[5432] }} dbname=')))
    def test_version_check_not_optional(self):
        self.assertFalse(verify(self.text.replace('NATIVE_POSTGRES_VERSION_MISMATCH', 'WRONG_VERSION_ACCEPTED')))
    def test_must_verify_service_before_triage(self):
        self.assertFalse(verify(self.text.replace('name: Start and verify native PostgreSQL 16 without containers','name: Native PostgreSQL omitted')))
