# 产季互助预付与实际损失份额结清

《互助约定与往来凭证》说明对接方已有的资料字段及业务口径。`data/source_records.json` 保存可供本地使用的合成记录，无真实机构数据和凭据。
使用 Python 3.11，资料读取、联调及测试只用标准库。用 `python3 -m json.tool data/source_records.json` 查阅本地交换记录。

在项目根目录运行 `python3 -m unittest discover -s tests -v` 检查合成资料的关联关系及已有的联调行为。
