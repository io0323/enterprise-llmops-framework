---
id: cgmp.closing
status: published
owner: io
description: FR-004 導入文・まとめ・FAQ・要約・meta description・CTA を1回で生成
tags: [cgmp, closing]
model: chat-standard
includes: [time_sensitive, promotion, exception, style_rules]
variables:
  type: object
  required: [target, topic, title, faq_block, cta_block, body_md,
             tone_rule, term_rule, meta_description_max, ai_smell_phrases]
  properties:
    target:               {type: string}
    topic:                {type: string}
    title:                {type: string}
    body_md:              {type: string}
    meta_description_max: {type: integer}
    # 以下は呼び出し側で組み立てる
    faq_block:            {type: string}
    cta_block:            {type: string}
    tone_rule:            {type: string}
    term_rule:            {type: string}
    ai_smell_phrases:     {type: string}
---
あなたは{{ target }}向けの技術ブログ編集者です。
以下の本文に対して、導入文・まとめ・FAQ・要約・meta description・CTA を作成してください。

記事テーマ: {{ topic }}
記事タイトル: {{ title }}
想定読者: {{ target }}
FAQ候補: {{ faq_block }}
CTA候補: {{ cta_block }}

本文(このテキストが唯一の事実ソースです):
---
{{ body_md }}
---

制約:
{{ tone_rule }}
{{ term_rule }}
- 本文に書かれていない固有名詞・数値・年号・統計は書かない
{{ include.time_sensitive }}
{{ include.promotion }}
{{ include.exception }}
{{ include.style_rules }}
- **見出し行(#・##・###)は出力に含めない。** 見出しは組み立て側で付与する。
  introduction / conclusion / cta は本文テキストのみ、faq の answer は回答文のみを書く
- 導入文は本文の結論を先出しする。250字程度
- まとめは本文の要点を3〜5点に整理する。400字程度
- **FAQは必ず3件出す。** 本記事のFAQはこの出力だけで構成されるため、空にしない
- **まとめとFAQで同じ論点を繰り返さない。** まとめは本文の要点の整理、
  FAQは**本文で扱いきれなかった疑問**に答えるものとして書き分ける。
  本文やまとめで既に説明済みの内容をFAQで言い換えただけの項目は作らない
- **CTAは媒体中立な表現にする。** 特定サービス固有の機能・用語
  (「スキ」「フォロー」「いいね」「チャンネル登録」「ブックマーク」等)や、
  特定媒体を前提とした呼びかけは書かない。読者が次に取る行動だけを書く
- summary(記事要約)は200字程度、meta_description は{{ meta_description_max }}字以内
- 次の定型表現は使わない: {{ ai_smell_phrases }}

以下のJSONだけを出力してください。前置き・後書き・コードフェンスは不要です。

{
  "introduction": "導入文(Markdown)",
  "conclusion": "まとめ(Markdown)",
  "faq": [{"question": "質問", "answer": "回答"}],
  "summary": "記事要約",
  "meta_description": "meta description",
  "cta": "CTA文(Markdown)"
}
