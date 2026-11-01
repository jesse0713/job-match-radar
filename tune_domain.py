# -*- coding: utf-8 -*-
"""调整领域词权重：'金融' 必须在高权重，否则最对口岗位会被降权。"""
import json
from pathlib import Path

p = Path(__file__).resolve().parent / "config.json"
cfg = json.loads(p.read_text(encoding="utf-8-sig"))

cfg["domain_terms"] = {
    "high": ["私人银行", "财富管理", "家族信托", "资产管理", "资管", "信托", "跨境", "证券", "银行",
             "保险", "理财", "基金", "养老金", "资产配置", "高净值", "金融", "金融科技", "投融资", "资管科技"],
    "mid": ["支付", "信贷", "风控", "合规", "反洗钱", "CRM", "客户经营", "零售金融", "对公", "投顾",
            "智能投顾", "智能客服", "核保", "理赔", "银保", "财富", "经纪", "清算", "征信"],
    "low": ["供应链金融", "消费金融", "融资租赁", "担保", "小贷"],
}
cfg["scoring"]["min_score_to_push"] = 50
p.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
print("config updated; high terms:", len(cfg["domain_terms"]["high"]))
