"""Coverage-first author revision; the v1 schema and mechanical gates stay fixed."""
from . import summary_faithfulness as v1

RECIPE = "echo-summary-faithfulness-v2"
SUMMARY_SCHEMA = v1.SUMMARY_SCHEMA
inspect_draft = v1.inspect_draft
validate_synthetic_window = v1.validate_synthetic_window

SYSTEM_PROMPT = v1.SYSTEM_PROMPT + """

本版整理流程：只提交最终 JSON，不输出中间草稿或复核过程。
先读完整个当前窗口，辨认相互独立的事情、关系和明确表达的感受，再组织摘要。
不要边读前几段边耗尽事实名额，让后面的独立事件只能成为前面事情的附注。

对同一事件，把先前安排、改动和最终状态放在一起理解。最终已取消的事件以取消状态交付，
文字保留原先安排和变更经过；不能再并列一条仍有效的同一计划。日期/主体的错误另存纠正历史。
不同的行动即便紧挨着也分别表达，尤其是已经完成的行动与打算以后做的行动。
一个失败及其后续修复是过程，已通过本地检查与尚未发布是不同事实，不能互相替代。

逐个回看来源回合，检查独立的已发生事项、明确否定、决定、更正、重要称呼和感受有没有保留。
同一回合有两件事情时，引用过该回合不代表两件事都写了；金额或数量也不能只留下它与别事的区别。
可合并反复表达和省去非关键布景，不能为短而漏掉另一件明确完成的独立事情。
金额、数量、地点、人物和版本必须来自当前窗口的明确表述，不能凭常识、人数或别的任务补全。

最后核对时间与出处：完整日期必须在本项有效引用中有相应事件的依据。只有月日、季节等
部分时间时，把有依据的部分写在正文，完整日期字段为 null；不为了填字段而补全年份。
引用同段的另一个完整日期不能证明本事件日期。若更正的主体或对象依赖前文，将必要前文一起引用。
明确没发生的事实放在 facts；uncertainties 只放资料未确定的部分，不能拿已知否定填未知清单。
"""
