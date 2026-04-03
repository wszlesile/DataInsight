import json
import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / 'src'
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from config.database import SessionLocal, init_db
from dto import DataSourceSchema, PropertySchema
from model import InsightDatasource, InsightNamespace, InsightNsRelDatasource


DEFAULT_CONVERSATION_ID = 0
SEED_NAMESPACE_USERNAME = 'seed'
SEED_NAMESPACE_NAME = '默认测试数据源空间'


def get_test_local_file_datasource_schema() -> DataSourceSchema:
    """销售记录文件的真实元数据定义。"""
    return DataSourceSchema(
        name='销售记录文件元数据',
        description='销售记录文件包含月度销售明细，可用于销售趋势、产品表现和区域分布分析。',
        properties={
            '月份': PropertySchema(type='string', description='销售月份，格式通常为 YYYY-MM', example='2024-10'),
            '产品名称': PropertySchema(type='string', description='产品名称', example='产品A'),
            '销售额(元)': PropertySchema(type='number', description='销售金额，单位为元', example=128000.5),
            '销量': PropertySchema(type='integer', description='销售数量', example=250),
            '销售单价': PropertySchema(type='number', description='产品销售单价', example=500),
            '区域': PropertySchema(type='string', description='销售区域名称', example='华东'),
        },
        required=['月份', '产品名称', '销售额(元)', '区域'],
    )


def get_test_local_sql_alarm_record_schema() -> DataSourceSchema:
    """报警记录表的真实元数据定义。"""
    return DataSourceSchema(
        name='报警记录表元数据',
        description='报警记录表保存报警事件及其时间、类型、优先级和测点信息，可用于报警数量统计、明细查询和异常分析。',
        properties={
            'id': PropertySchema(type='integer', description='报警记录主键 ID', example=31),
            'ar_code': PropertySchema(type='string', description='报警编码', example='AR-20260224-001'),
            'tagname': PropertySchema(type='string', description='测点标签名', example='poly1-R101-TT'),
            'show_name': PropertySchema(type='string', description='测点显示名称', example='温度传感器1'),
            'description': PropertySchema(type='string', description='报警描述信息', example='温度超过上限阈值'),
            'alarm_type': PropertySchema(type='string', description='报警类型', example='高温报警'),
            'priority': PropertySchema(type='integer', description='报警优先级，数值越小优先级越高', example=1),
            'limit_condition': PropertySchema(type='string', description='报警触发条件', example='> 85°C'),
            'start_timestamp': PropertySchema(type='string', description='报警开始时间', example='2026-02-24 08:30:15+00'),
            'new_value': PropertySchema(type='number', description='报警触发时的新值', example=92.5),
            'disappeared_timestamp': PropertySchema(type='string', description='报警消失时间', example='2026-02-24 09:15:23+00'),
            'created_time': PropertySchema(type='string', description='记录创建时间', example='2026-02-24 08:30:15+00'),
        },
        required=['id', 'tagname', 'alarm_type', 'priority', 'start_timestamp'],
    )


def get_test_local_sql_alarm_treatment_schema() -> DataSourceSchema:
    """报警处置工单表的真实元数据定义。"""
    return DataSourceSchema(
        name='报警处置工单表元数据',
        description='报警处置工单表记录报警闭环处理过程，可用于分析处理状态、原因、处置措施和完成时效。',
        properties={
            'id': PropertySchema(type='integer', description='工单主键 ID', example=1),
            'work_order_code': PropertySchema(type='string', description='工单编号', example='WO-20260224-001'),
            'alarm_record_id': PropertySchema(type='integer', description='关联的报警记录主键 ID', example=1),
            'alarm_status': PropertySchema(type='integer', description='报警处理状态编码', example=2),
            'completion_time': PropertySchema(type='string', description='处理完成时间', example='2026-02-24 11:30:00+00'),
            'alarm_cause': PropertySchema(type='string', description='报警原因分析', example='散热风扇故障导致温度升高'),
            'corrective_action': PropertySchema(type='string', description='纠正措施', example='更换散热风扇，清理灰尘'),
            'remarks': PropertySchema(type='string', description='处理备注', example='已恢复正常运行'),
            'attachments': PropertySchema(type='string', description='附件文件名或路径', example='fan_inspection_report.pdf'),
            'created_time': PropertySchema(type='string', description='工单创建时间', example='2026-02-24 09:20:00+00'),
        },
        required=['id', 'work_order_code', 'alarm_record_id', 'alarm_status'],
    )


def get_or_create_seed_namespace(session) -> InsightNamespace:
    """获取固定的测试种子空间，不存在时自动创建。"""
    namespace = session.query(InsightNamespace).filter(
        InsightNamespace.username == SEED_NAMESPACE_USERNAME,
        InsightNamespace.name == SEED_NAMESPACE_NAME,
    ).first()
    if namespace is None:
        namespace = InsightNamespace(
            username=SEED_NAMESPACE_USERNAME,
            name=SEED_NAMESPACE_NAME,
            is_deleted=0,
        )
        session.add(namespace)
        session.flush()
    elif namespace.is_deleted:
        namespace.is_deleted = 0
        session.flush()
    return namespace


def _upsert_default_conversation_datasource(
    session,
    namespace_id: int,
    sort_no: int,
    datasource_type: str,
    datasource_name: str,
    knowledge_tag: str,
    metadata_schema: DataSourceSchema,
    datasource_config_json: dict,
) -> dict:
    """把一条默认会话资源写入数据源表和会话数据源关系表。"""
    schema_json = json.dumps(metadata_schema.model_dump(), ensure_ascii=False)
    config_json = json.dumps(datasource_config_json, ensure_ascii=False)

    datasource = session.query(InsightDatasource).filter(
        InsightDatasource.insight_namespace_id == namespace_id,
        InsightDatasource.datasource_type == datasource_type,
        InsightDatasource.datasource_name == datasource_name,
    ).first()

    if datasource is None:
        datasource = InsightDatasource(
            insight_namespace_id=namespace_id,
            datasource_type=datasource_type,
            datasource_name=datasource_name,
            knowledge_tag=knowledge_tag,
            datasource_schema=schema_json,
            datasource_config_json=config_json,
            is_deleted=0,
        )
        session.add(datasource)
        session.flush()
    else:
        datasource.knowledge_tag = knowledge_tag
        datasource.datasource_schema = schema_json
        datasource.datasource_config_json = config_json
        datasource.is_deleted = 0
        session.flush()

    relation = session.query(InsightNsRelDatasource).filter(
        InsightNsRelDatasource.insight_namespace_id == namespace_id,
        InsightNsRelDatasource.insight_conversation_id == DEFAULT_CONVERSATION_ID,
        InsightNsRelDatasource.datasource_id == datasource.id,
    ).first()

    if relation is None:
        relation = InsightNsRelDatasource(
            insight_namespace_id=namespace_id,
            insight_conversation_id=DEFAULT_CONVERSATION_ID,
            datasource_id=datasource.id,
            is_active=1,
            sort_no=sort_no,
            is_deleted=0,
        )
        session.add(relation)
        session.flush()
    else:
        relation.sort_no = sort_no
        relation.is_deleted = 0
        session.flush()

    return {
        'datasource_id': datasource.id,
        'datasource_name': datasource.datasource_name,
        'datasource_type': datasource.datasource_type,
        'knowledge_tag': datasource.knowledge_tag,
        'relation_id': relation.id,
    }


def seed_default_conversation_zero_datasources(namespace_id: int) -> list[dict]:
    """
    为指定空间初始化默认的 `conversation_id == 0` 数据源资源。

    这些默认关系会在后续真实会话没有绑定任何数据源时，
    作为该空间的默认数据源集合被自动复制到会话级关系表。
    """
    init_db()
    session = SessionLocal()
    try:
        namespace = session.query(InsightNamespace).filter(
            InsightNamespace.id == namespace_id,
            InsightNamespace.is_deleted == 0,
        ).first()
        if namespace is None:
            raise ValueError(f'洞察空间不存在: namespace_id={namespace_id}')

        seeded_items = [
            _upsert_default_conversation_datasource(
                session=session,
                namespace_id=namespace_id,
                sort_no=1,
                datasource_type='local_file',
                datasource_name='销售记录文件',
                knowledge_tag='sales_file',
                metadata_schema=get_test_local_file_datasource_schema(),
                datasource_config_json={'file_path': 'D:/PycharmProjects/DataInsight/xiaoshou.csv'},
            ),
            _upsert_default_conversation_datasource(
                session=session,
                namespace_id=namespace_id,
                sort_no=2,
                datasource_type='table',
                datasource_name='报警记录表',
                knowledge_tag='alarm_record_table',
                metadata_schema=get_test_local_sql_alarm_record_schema(),
                datasource_config_json={'table_name': 'baojingjilubiao'},
            ),
            _upsert_default_conversation_datasource(
                session=session,
                namespace_id=namespace_id,
                sort_no=3,
                datasource_type='table',
                datasource_name='报警处置工单表',
                knowledge_tag='alarm_work_order_table',
                metadata_schema=get_test_local_sql_alarm_treatment_schema(),
                datasource_config_json={'table_name': 'baojinggongdanbiao'},
            ),
        ]
        session.commit()
        return seeded_items
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def seed_default_namespace_resources() -> dict:
    """创建固定种子空间，并写入默认会话资源。"""
    init_db()
    session = SessionLocal()
    try:
        namespace = get_or_create_seed_namespace(session)
        session.commit()
        namespace_id = namespace.id
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()

    seeded_items = seed_default_conversation_zero_datasources(namespace_id)
    return {
        'namespace_id': namespace_id,
        'namespace_name': SEED_NAMESPACE_NAME,
        'default_conversation_id': DEFAULT_CONVERSATION_ID,
        'datasources': seeded_items,
    }


class DefaultConversationDatasourceSeedTestCase(unittest.TestCase):
    """验证默认 `conversation_id == 0` 数据源资源可以稳定入库。"""

    def test_seed_default_namespace_resources(self) -> None:
        result = seed_default_namespace_resources()
        self.assertTrue(result['namespace_id'] > 0)
        self.assertEqual(result['default_conversation_id'], DEFAULT_CONVERSATION_ID)
        self.assertEqual(len(result['datasources']), 3)

        session = SessionLocal()
        try:
            datasource_rows = session.query(InsightDatasource).filter(
                InsightDatasource.insight_namespace_id == result['namespace_id'],
                InsightDatasource.is_deleted == 0,
            ).order_by(InsightDatasource.id.asc()).all()
            self.assertEqual(len(datasource_rows), 3)

            relation_rows = session.query(InsightNsRelDatasource).filter(
                InsightNsRelDatasource.insight_namespace_id == result['namespace_id'],
                InsightNsRelDatasource.insight_conversation_id == DEFAULT_CONVERSATION_ID,
                InsightNsRelDatasource.is_deleted == 0,
            ).order_by(InsightNsRelDatasource.sort_no.asc()).all()
            self.assertEqual(len(relation_rows), 3)

            datasource_names = {row.datasource_name for row in datasource_rows}
            self.assertEqual(
                datasource_names,
                {'销售记录文件', '报警记录表', '报警处置工单表'},
            )

            first_schema = json.loads(datasource_rows[0].datasource_schema)
            self.assertIn('properties', first_schema)
            self.assertTrue(first_schema['properties'])
        finally:
            session.close()


if __name__ == '__main__':
    unittest.main()
