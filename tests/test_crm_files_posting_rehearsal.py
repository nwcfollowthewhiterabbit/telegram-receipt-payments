from __future__ import annotations

from contextlib import contextmanager, redirect_stdout
import copy
import datetime as dt
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from scripts import rehearse_crm_files_posting as rehearsal


RUN = "12345678123442348234123456789abc"
SHA = "a"*40
SOURCE_GUID = "11111111-2222-3333-4444-555555555555"
CLONE_GUID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"


def evidence():
    return dict(run_id=RUN,restore_database=rehearsal.backup.CLONE_PREFIX+RUN,
                execution_sha=SHA,ci_run_id="123",verified=True,stage="verified",
                source_schema_matches=True,backup_checksum_verified=True,dbcc_physical_only_completed=True,
                server_fingerprint=rehearsal.backup.fingerprint("PRIVATE_SERVER_SENTINEL"),
                source_database_guid=SOURCE_GUID,clone_database_guid=CLONE_GUID,clone_database_id=9,
                clone_create_date="2026-11-11 12:00:00",restore_file_paths_sha256="b"*64,
                record_sha256="c"*64,schema={"schema_sha256":"d"*64,"catalog_rows":10})


def identity():
    return dict(owner=RUN,database_guid=CLONE_GUID,database_id=9,create_date=dt.datetime(2026,11,11,12),
                restore_file_paths_sha256="b"*64,state_desc="ONLINE",user_access_desc="RESTRICTED_USER",
                public_connect_denied=True,is_broker_enabled=False,is_trustworthy_on=False,is_db_chaining_on=False)


class FakeAdmin:
    def __init__(self):
        self.clone=identity()
        self.info=dict(is_sysadmin=1,server_identity="PRIVATE_SERVER_SENTINEL",source_guid=SOURCE_GUID)
        self.schema=evidence()["schema"]

    def metadata(self): return self.info
    def clone_identity(self,name): return self.clone
    def schema_digest(self,name): return self.schema


@contextmanager
def value_context(value):
    yield value


class CloneGuardTests(unittest.TestCase):
    def test_verified_metadata_and_exact_source_are_required(self):
        self.assertEqual(rehearsal.validate_evidence(evidence(),SHA,"123",execute=True),evidence()["restore_database"])
        for changes in ({"restore_database":"Terrasoft"},{"run_id":"0"*32},{"verified":False},
                        {"backup_checksum_verified":False},{"source_schema_matches":False},
                        {"execution_sha":"e"*40},{"ci_run_id":"999"}):
            with self.subTest(changes=changes),self.assertRaises(rehearsal.RehearsalError):
                rehearsal.validate_evidence({**evidence(),**changes},SHA,"123",execute=True)

    def test_clone_provenance_and_every_isolation_flag_are_checked(self):
        self.assertEqual(rehearsal.verify_owned_clone(FakeAdmin(),evidence()),evidence()["restore_database"])
        changes=[{"owner":"different"},{"database_guid":SOURCE_GUID},{"database_id":1},
                 {"create_date":dt.datetime(2026,11,10)},{"restore_file_paths_sha256":"x"},
                 {"state_desc":"OFFLINE"},{"user_access_desc":"MULTI_USER"},
                 {"public_connect_denied":False},{"is_broker_enabled":True},
                 {"is_trustworthy_on":True},{"is_db_chaining_on":True}]
        for change in changes:
            admin=FakeAdmin()
            admin.clone.update(change)
            with self.subTest(change=change),self.assertRaises(rehearsal.RehearsalError):
                rehearsal.verify_owned_clone(admin,evidence())

    def test_source_and_schema_change_are_refused(self):
        for target in ("source","server","schema"):
            admin=FakeAdmin()
            if target=="source": admin.info["source_guid"]=CLONE_GUID
            if target=="server": admin.info["server_identity"]="another server"
            if target=="schema": admin.schema={"schema_sha256":"changed"}
            with self.subTest(target=target),self.assertRaises(rehearsal.RehearsalError):
                rehearsal.verify_owned_clone(admin,evidence())

    def test_failed_production_guard_never_constructs_clone_engine(self):
        with patch.object(rehearsal.backup,"open_admin",side_effect=RuntimeError("private connection")), \
             patch.object(rehearsal,"create_engine") as create:
            with self.assertRaises(RuntimeError):
                with rehearsal.open_owned_clone(evidence()): pass
        create.assert_not_called()

    def test_connection_itself_must_match_owned_clone(self):
        connection=MagicMock()
        row={**identity(),"name":evidence()["restore_database"],"server_identity":"PRIVATE_SERVER_SENTINEL"}
        connection.execute.return_value.mappings.return_value.one.return_value=row
        connection.execute.return_value.scalars.return_value.all.return_value=[RUN]
        connection.execute.return_value.scalar_one.return_value=1
        rehearsal.verify_connection_identity(connection,evidence())
        row["name"]="Terrasoft"
        with self.assertRaisesRegex(rehearsal.RehearsalError,"connection_identity"):
            rehearsal.verify_connection_identity(connection,evidence())

    def test_engine_url_is_derived_only_after_guard_and_never_reconfigures_connector(self):
        from sqlalchemy.engine import make_url
        connection=MagicMock()
        connector=MagicMock()
        connector.settings.terrasoft_database="Terrasoft"
        connector.verify_connection_target.return_value="Terrasoft"
        connector._validate_live_target.return_value=make_url("mssql+pymssql://user:secret@example.invalid/Terrasoft")
        engine=MagicMock()
        engine.connect.return_value.__enter__.return_value=connection
        with patch.object(rehearsal.backup,"open_admin",return_value=value_context(FakeAdmin())), \
             patch.object(rehearsal,"TerrasoftMssqlConnector",return_value=connector), \
             patch.object(rehearsal,"create_engine",return_value=engine) as create, \
             patch.object(rehearsal,"verify_connection_identity"), \
             patch.object(rehearsal,"verify_local_trigger_graph",return_value=20):
            with rehearsal.open_owned_clone(evidence()) as result:
                self.assertIs(result[0],connection)
        url=create.call_args.args[0]
        self.assertEqual(url.database,evidence()["restore_database"])
        self.assertEqual(connector.settings.terrasoft_database,"Terrasoft")
        self.assertEqual(create.call_args.kwargs["hide_parameters"],True)
        engine.dispose.assert_called_once()


class GraphConnection:
    def __init__(self,definition="CREATE TRIGGER test ON dbo.fixture AFTER INSERT AS UPDATE dbo.fixture SET x=1",
                 dependency=None,kind="TR"):
        self.definition,self.dependency,self.kind=definition,dependency,kind
        self.sql=[]

    def execute(self,statement,params=None):
        sql=str(statement)
        self.sql.append(sql)
        result=MagicMock()
        if "SELECT OBJECT_ID" in sql: result.scalar_one.return_value=1
        elif "FROM sys.all_objects" in sql:
            result.mappings.return_value.one.return_value={"type":self.kind,"definition":self.definition,
                                                          "qualified_name":"dbo.synthetic"}
        elif "FROM sys.triggers" in sql: result.scalars.return_value.all.return_value=[]
        elif "FROM sys.sql_expression_dependencies" in sql or "FROM sys.dm_sql_referenced_entities" in sql:
            result.mappings.return_value.all.return_value=[self.dependency] if self.dependency else []
        else: raise AssertionError("unexpected graph query")
        return result


class DependencyGuardTests(unittest.TestCase):
    def test_local_static_modules_pass_using_only_select(self):
        c=GraphConnection()
        self.assertEqual(rehearsal.verify_local_trigger_graph(c),1)
        self.assertTrue(all(sql.lstrip().startswith("SELECT") for sql in c.sql))

    def test_external_and_dynamic_execution_are_blocked(self):
        for definition in ("EXEC dbo.send_mail", "SELECT * FROM OPENQUERY(remote,'x')", "EXECUTE(@sql)",
                           "BACKUP DATABASE example TO DISK='x'", "SEND ON CONVERSATION @id MESSAGE TYPE X ('x')"):
            with self.subTest(definition=definition),self.assertRaises(rehearsal.RehearsalError):
                rehearsal.verify_local_trigger_graph(GraphConnection(definition=definition))

    def test_cross_database_unresolved_ambiguous_and_clr_are_blocked(self):
        base=dict(referenced_id=1,referenced_database_name=None,referenced_server_name=None,
                  is_ambiguous=False,referenced_class=1,referenced_entity_name="fixture",is_updated=False)
        for change in ({"referenced_database_name":"Terrasoft"},{"referenced_server_name":"remote"},
                       {"referenced_id":None},{"is_ambiguous":True}):
            with self.subTest(change=change),self.assertRaises(rehearsal.RehearsalError):
                rehearsal.verify_local_trigger_graph(GraphConnection(dependency={**base,**change}))
        with self.assertRaises(rehearsal.RehearsalError):
            rehearsal.verify_local_trigger_graph(GraphConnection(kind="FS"))

    def test_comments_and_string_literals_do_not_claim_external_execution(self):
        definition="-- EXEC old_example\nSELECT 'OPENQUERY(''legacy'')'; /* USE old */"
        self.assertEqual(rehearsal.verify_local_trigger_graph(GraphConnection(definition=definition)),1)


class RehearsalExecutionTests(unittest.TestCase):
    def test_all_fixture_writes_roll_back_and_are_verified(self):
        connection=MagicMock()
        scenario=MagicMock(spec=rehearsal.Scenario)
        scenario.run.return_value={"checks":["synthetic"]}
        with patch.object(rehearsal,"Scenario",return_value=scenario), \
             patch.object(rehearsal,"verify_connection_identity"):
            result=rehearsal.execute_rehearsal(connection,MagicMock(),evidence(),RUN)
        connection.begin.return_value.rollback.assert_called_once()
        connection.begin.return_value.commit.assert_not_called()
        scenario.assert_rolled_back.assert_called_once()
        self.assertTrue(result["all_fixture_writes_rolled_back"])
        self.assertTrue(result["clone_retained"])

    def test_exception_still_rolls_back_without_commit(self):
        connection=MagicMock()
        scenario=MagicMock(spec=rehearsal.Scenario)
        scenario.run.side_effect=RuntimeError("failed during synthetic writes")
        with patch.object(rehearsal,"Scenario",return_value=scenario), \
             patch.object(rehearsal,"verify_connection_identity"),self.assertRaises(RuntimeError):
            rehearsal.execute_rehearsal(connection,MagicMock(),evidence(),RUN)
        connection.begin.return_value.rollback.assert_called_once()
        connection.begin.return_value.commit.assert_not_called()

    def test_default_cli_never_executes_scenarios_or_writes_evidence(self):
        output=io.StringIO()
        with patch.object(rehearsal.backup,"read_verified_evidence",return_value=evidence()), \
             patch.object(rehearsal,"open_owned_clone",return_value=value_context((MagicMock(),MagicMock(),10))), \
             patch.object(rehearsal,"execute_rehearsal") as execute, \
             patch.object(rehearsal.backup,"Journal") as journal,redirect_stdout(output):
            self.assertEqual(rehearsal.main(["--verified-backup","unused.json"]),0)
        execute.assert_not_called()
        journal.assert_not_called()
        self.assertEqual(json.loads(output.getvalue())["mutations"],False)

    def test_execute_missing_or_wrong_ci_rejected_before_connection(self):
        for args in ([],["--execution-sha","f"*40,"--ci-run-id","123"]):
            with patch.object(rehearsal.backup,"read_verified_evidence",return_value=evidence()), \
                 patch.object(rehearsal,"open_owned_clone") as connect,redirect_stdout(io.StringIO()):
                self.assertEqual(rehearsal.main(["--verified-backup","unused.json","--execute",*args]),1)
            connect.assert_not_called()

    def test_final_evidence_matches_deployment_contract_and_errors_are_sanitized(self):
        for fail in (False,True):
            with tempfile.TemporaryDirectory() as directory:
                output=io.StringIO()
                effect=RuntimeError("PRIVATE_PASSWORD_AND_BUSINESS_ROW") if fail else None
                with patch.object(rehearsal.backup,"read_verified_evidence",return_value=evidence()), \
                     patch.object(rehearsal,"open_owned_clone",return_value=value_context((MagicMock(),MagicMock(),10))), \
                     patch.object(rehearsal,"execute_rehearsal",return_value={"all_fixture_writes_rolled_back":True},side_effect=effect), \
                     redirect_stdout(output):
                    code=rehearsal.main(["--verified-backup","unused.json","--execute","--execution-sha",SHA,
                                         "--ci-run-id","123","--evidence-dir",directory])
                self.assertEqual(code,1 if fail else 0)
                final=json.loads(sorted(Path(directory).glob("*.json"))[-1].read_text())
                self.assertEqual(final["execution_sha"],SHA)
                self.assertEqual(final["passed"],not fail)
                self.assertNotIn("PRIVATE_",output.getvalue())
                self.assertNotIn("PRIVATE_",json.dumps(final))


if __name__ == "__main__":
    unittest.main()
