import json
from typing import Dict, List

from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel, Field

from config.config import Config


class CustomContext(BaseModel):
    """自定义上下文"""
    username: str


def get_history_message():
    return HumanMessage("无历史对话记录")


class SchemaProperty(BaseModel):
    property_type: str = Field(description='属性类型')
    name: str = Field(description='属性名称')
    description: str = Field(description='属性描述')


class DataSourceSchema(BaseModel):
    schema_type: int = Field(description='数据类型')
    identify:str = Field(description='数据标识')
    name: str = Field(description='数据名称')
    description: str = Field(description='数据描述')
    properties: Dict[str, SchemaProperty] = Field(default_factory=dict, description='数据属性')
    required: list = Field(default_factory=list, description='必填属性')


def get_system_config_messages():
    path = Config.TEMP_DIR
    return HumanMessage(
        f'''系统配置信息：
- 图表文件临时保存目录：{path}
- **重要**：在生成的代码中，必须将此目录赋值给变量 temp_dir，例如：temp_dir = "{path}"'''
    )


def get_test_local_file_datasource_schema() -> DataSourceSchema:
    properties = {
        '月份': SchemaProperty(property_type='string', name='月份', description='月份'),
        '产品名称': SchemaProperty(property_type='string', name='产品名称', description='产品名称'),
        '销售额(元)': SchemaProperty(property_type='double', name='销售额', description='销售额(元)'),
        '销量': SchemaProperty(property_type='integer', name='销量', description='销量'),
        '销售单价': SchemaProperty(property_type='double', name='销售单价', description='销售单价'),
        '区域': SchemaProperty(property_type='string', name='区域', description='区域'),
    }
    s = DataSourceSchema(
        schema_type=1,
        identify='xiaoshou',
        name='销售记录表',
        description='统计了各个年月份各个产品的销售额',
        properties=properties,
        required=[]
    )
    return s
def get_test_local_sql_datasource_schema() -> List[DataSourceSchema]:
    # 报警记录表 schema
    alarm_record_properties = {
        'id': SchemaProperty(property_type='integer', name='自增编码', description='自增编码，主键'),
        'ar_code': SchemaProperty(property_type='string', name='报警编码', description='报警编码，规则：ARCode + YYYYMMDD + 4位日序号'),
        'tagname': SchemaProperty(property_type='string', name='位号', description='位号'),
        'show_name': SchemaProperty(property_type='string', name='报警名称', description='报警名称'),
        'description': SchemaProperty(property_type='string', name='报警描述', description='报警描述'),
        'alarm_type': SchemaProperty(property_type='string', name='报警类型', description='报警类型：超限报警、ON/OFF报警'),
        'priority': SchemaProperty(property_type='integer', name='报警优先级', description='报警优先级：1-10，1为最高'),
        'limit_condition': SchemaProperty(property_type='string', name='报警条件', description='报警条件'),
        'start_timestamp': SchemaProperty(property_type='string', name='报警产生时间', description='报警产生时间'),
        'new_value': SchemaProperty(property_type='double', name='触发报警的值', description='触发报警的值'),
        'disappeared_timestamp': SchemaProperty(property_type='string', name='报警消除时间', description='报警消除时间'),
        'created_time': SchemaProperty(property_type='string', name='创建时间', description='创建时间'),
    }
    alarm_record_schema = DataSourceSchema(
        schema_type=3,
        identify='baojingjilubiao',
        name='报警记录表',
        description='记录设备或系统的报警事件，包含报警编码、位号、报警类型、优先级、触发值等信息',
        properties=alarm_record_properties,
        required=['id', 'ar_code', 'tagname', 'start_timestamp']
    )

    # 报警工单表 schema
    alarm_treatment_properties = {
        'id': SchemaProperty(property_type='integer', name='自增编码', description='自增编码，主键'),
        'work_order_code': SchemaProperty(property_type='string', name='工单编码', description='工单编码，规则：AROrder + YYYYMMDD + 4位日序号'),
        'alarm_record_id': SchemaProperty(property_type='integer', name='报警记录ID', description='报警编码，关联报警记录表ID'),
        'alarm_status': SchemaProperty(property_type='integer', name='工单处理状态', description='工单处理状态：1-未处理，2-已确认'),
        'completion_time': SchemaProperty(property_type='string', name='处理完成时间', description='处理完成时间'),
        'alarm_cause': SchemaProperty(property_type='string', name='问题原因', description='问题原因'),
        'corrective_action': SchemaProperty(property_type='string', name='纠正预防措施', description='纠正预防措施'),
        'remarks': SchemaProperty(property_type='string', name='备注', description='备注'),
        'attachments': SchemaProperty(property_type='string', name='附件', description='附件，存储JSON格式'),
        'created_time': SchemaProperty(property_type='string', name='创建时间', description='创建时间'),
    }
    alarm_treatment_schema = DataSourceSchema(
        schema_type=3,
        identify='baojinggongdanbiao',
        name='报警工单表',
        description='记录报警工单的处理信息，包含工单编码、报警记录ID、处理状态、问题原因、纠正预防措施等',
        properties=alarm_treatment_properties,
        required=['id', 'work_order_code', 'alarm_record_id']
    )

    return [alarm_record_schema, alarm_treatment_schema]
def get_datasource_messages(user_message: str):
    # rag 召回 todo
    # 本地文件数据源
    local_schema: DataSourceSchema = get_test_local_file_datasource_schema()
    # 数据库数据源（报警记录表、报警工单表）
    sql_schemas: List[DataSourceSchema] = get_test_local_sql_datasource_schema()

    # 构建符合数据源上下文格式的消息
    datasource_list = [
        {
            "数据源类型": "本地文件",
            "数据源名称": local_schema.name,
            "数据源标识": "D:\\PycharmProjects\\DataInsight\\xiaoshou.csv",
            "元数据Schema": local_schema.model_dump()
        }
    ]

    # 添加数据库数据源
    for sql_schema in sql_schemas:
        datasource_list.append({
            "数据源类型": "数据库",
            "数据源名称": sql_schema.name,
            "数据源标识": sql_schema.identify,
            "元数据Schema": sql_schema.model_dump()
        })

    datasource_context = {"数据源列表": datasource_list}
    datasource_json = json.dumps(datasource_context, indent=2, ensure_ascii=False)
    schema = HumanMessage(f'数据源上下文：\n{datasource_json}')
    return schema
