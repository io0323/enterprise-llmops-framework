---
id: cgmp.section_no_context
status: published
owner: io
description: 本文セクション生成(context なし(RAG が引けなかった場合))
tags: [cgmp, writer]
model: chat-standard
includes: [no_hallucination, time_sensitive, promotion, exception, style_rules]
constraints:
  # Phase 2 の groundedness 評価がこの宣言を根拠に検証する(設計 §3)
  require_grounding: false
  forbid_outside_context: true
variables:
  type: object
  required: [target, topic, intent, title, outline_all, section_heading, points,
             context, tone_rule, term_rule, previous_ref, target_chars, ai_smell_phrases]
  properties:
    target:           {type: string}
    topic:            {type: string}
    intent:           {type: string}
    title:            {type: string}
    section_heading:  {type: string}
    context:          {type: string}
    target_chars:     {type: integer}
    # 以下は呼び出し側で組み立てる。パラメータ依存のテキストは fragment にしない(N-008)
    outline_all:      {type: string}
    points:           {type: string}
    tone_rule:        {type: string}
    term_rule:        {type: string}
    previous_ref:     {type: string}
    ai_smell_phrases: {type: string}
---
あなたは{{ target }}向けの技術ブログ執筆者です。

記事テーマ: {{ topic }}
検索意図: {{ intent }}
記事タイトル: {{ title }}
記事全体のアウトライン(→ が今回執筆するセクション):
{{ outline_all }}

今回執筆するセクション: {{ section_heading }}
このセクションの論点:
{{ points }}

参照可能な事実(context):
{{ context }}

制約:
{{ tone_rule }}
{{ term_rule }}
{{ include.no_hallucination }}

{{ include.time_sensitive }}
{{ include.promotion }}
{{ include.exception }}
{{ include.style_rules }}
- 直前のセクション{{ previous_ref }}の内容を繰り返さない。
  他セクションで扱う論点には踏み込まない
- 目安{{ target_chars }}字。見出し行(##)は出力に含めない
- 箇条書き・表はMarkdown記法で使ってよい(H3見出し ### は使ってよい)
- 次の表現を使わない: {{ ai_smell_phrases }}

出力はMarkdown本文のみ。前置き・後書き・コードフェンスは禁止。
