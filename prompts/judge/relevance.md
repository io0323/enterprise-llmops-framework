---
id: judge.relevance
status: published
owner: io
description: 生成文が指示(見出し・論点)に答えているかを採点する
tags: [judge, relevance]
model: judge
includes: [judge_output]
variables:
  type: object
  required: [instruction, output]
  properties:
    instruction: {type: string}
    output:      {type: string}
---
あなたは編集レビュアーです。以下の「生成文」が「指示」に答えているかを採点してください。

# 指示
{{ instruction }}

# 生成文
{{ output }}

採点の基準:
- 1.0 = 指示された論点にすべて答えており、指示にない話題へ逸れていない
- 0.5 = 主要な論点には答えているが、欠落または余分な話題がある
- 0.0 = 指示と無関係な内容になっている
- **事実の正しさは採点対象にしない**(groundedness で見ている)
- violations には、欠落した論点と余分な話題を列挙する

{{ include.judge_output }}
