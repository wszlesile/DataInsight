import json
from typing import Dict

from langchain_core.messages import HumanMessage
from pydantic import BaseModel, Field


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
    name: str = Field(description='数据名称')
    description: str = Field(description='数据描述')
    properties: Dict[str, SchemaProperty] = Field(default_factory=dict, description='数据属性')
    required: list = Field(default_factory=list, description='必填属性')


def get_datasource_messages(user_message: str):
    # rag 召回
    datasource_message = json.dumps(DataSourceSchema.model_json_schema(), indent=2, ensure_ascii=False)
    properties={
            '月份': SchemaProperty(property_type='string', name='月份', description='月份'),
            '产品名称': SchemaProperty(property_type='string', name='产品名称', description='产品名称'),
            '销售额(元)': SchemaProperty(property_type='double', name='销售额', description='销售额(元)'),
            '销量': SchemaProperty(property_type='integer', name='销量', description='销量'),
            '销售单价': SchemaProperty(property_type='double', name='销售单价', description='销售单价'),
            '区域': SchemaProperty(property_type='string', name='区域', description='区域'),
        }
    s = DataSourceSchema(
        schema_type=1,
        name='销售记录表',
        description='统计了各个年月份份各个产品的销售额',
        properties=properties,
        required=[]
    )
    data_schema = json.dumps(s.model_dump(), indent=2, ensure_ascii=False)
    schemas = [HumanMessage(datasource_message),HumanMessage(data_schema)]
    return schemas
get_datasource_messages('')