import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from uuid import uuid4

import pandas as pd
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from werkzeug.datastructures import FileStorage

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / 'src'
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from agent.context_engineering import get_datasource_message
from config.database import Base
from model import InsightDatasource, InsightNamespace
from service.conversation_context_service import ConversationContextService
from service.insight_ns_rel_datasource_service import InsightNsRelDatasourceService


class ExcelMultiSheetDatasourceBackendTestCase(unittest.TestCase):
    def setUp(self):
        engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(bind=engine)
        session_factory = sessionmaker(autocommit=False, autoflush=False, bind=engine)
        self.session = session_factory()
        self.namespace = InsightNamespace(
            username='excel_sheet_test',
            name=f'excel-multi-sheet-{uuid4().hex[:8]}',
            is_deleted=0,
        )
        self.session.add(self.namespace)
        self.session.commit()
        self.session.refresh(self.namespace)

    def tearDown(self):
        self.session.close()

    def _build_excel_upload(self) -> FileStorage:
        buffer = io.BytesIO()
        with pd.ExcelWriter(buffer, engine='openpyxl') as writer:
            pd.DataFrame([
                {"order_id": "A001", "sales": 1200, "region": "east"},
                {"order_id": "A002", "sales": 900, "region": "south"},
            ]).to_excel(writer, sheet_name='detail', index=False)
            pd.DataFrame([
                {"region": "east", "total_sales": 1200},
                {"region": "south", "total_sales": 900},
            ]).to_excel(writer, sheet_name='summary', index=False)
        buffer.seek(0)
        return FileStorage(stream=buffer, filename='sales.xlsx')

    def test_upload_excel_creates_one_datasource_per_sheet(self):
        service = InsightNsRelDatasourceService(self.session)
        upload = self._build_excel_upload()

        with tempfile.TemporaryDirectory() as upload_dir:
            result = service.upload_file_datasource_to_namespace(
                insight_namespace_id=self.namespace.id,
                upload_file=upload,
                upload_dir=upload_dir,
            )

        self.assertTrue(result["success"])
        self.assertEqual(result["data"]["created_count"], 2)
        self.assertEqual(len(result["data"]["created_datasources"]), 2)

        datasources = self.session.query(InsightDatasource).filter(
            InsightDatasource.insight_namespace_id == self.namespace.id,
            InsightDatasource.is_deleted == 0,
        ).order_by(InsightDatasource.id.asc()).all()

        self.assertEqual(len(datasources), 2)

        configs = [json.loads(item.datasource_config_json) for item in datasources]
        self.assertEqual({config.get("sheet_name") for config in configs}, {"detail", "summary"})
        self.assertEqual(len({config.get("workbook_group_id") for config in configs}), 1)
        self.assertTrue(any("detail" in item.datasource_name for item in datasources))
        self.assertTrue(any("summary" in item.datasource_name for item in datasources))

        schema_by_sheet = {
            config["sheet_name"]: json.loads(datasource.datasource_schema)
            for datasource, config in zip(datasources, configs)
        }
        self.assertIn("order_id", schema_by_sheet["detail"]["properties"])
        self.assertIn("total_sales", schema_by_sheet["summary"]["properties"])

    def test_snapshot_and_runtime_message_include_sheet_name(self):
        service = InsightNsRelDatasourceService(self.session)
        upload = self._build_excel_upload()

        with tempfile.TemporaryDirectory() as upload_dir:
            result = service.upload_file_datasource_to_namespace(
                insight_namespace_id=self.namespace.id,
                upload_file=upload,
                upload_dir=upload_dir,
            )

        self.assertTrue(result["success"])
        datasource = self.session.query(InsightDatasource).filter(
            InsightDatasource.insight_namespace_id == self.namespace.id,
            InsightDatasource.datasource_name.like('%detail%'),
            InsightDatasource.is_deleted == 0,
        ).first()
        self.assertIsNotNone(datasource)

        snapshot_item = ConversationContextService(self.session)._build_datasource_snapshot_item(datasource)
        self.assertEqual(snapshot_item["sheet_name"], "detail")

        message = get_datasource_message(
            namespace_id=self.namespace.id,
            conversation_id=0,
            snapshot_override={
                "namespace_id": self.namespace.id,
                "selected_datasource_snapshot": [snapshot_item],
                "selected_datasource_ids": [datasource.id],
            },
        )

        self.assertIsNotNone(message)
        self.assertIn('"sheet_name": "detail"', message.content)
        self.assertIn("sheet_name", message.content)
        self.assertIn("load_local_file", message.content)


if __name__ == '__main__':
    unittest.main()
