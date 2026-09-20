---
id: cgmp.outline
status: published
owner: io
description: FR-002 アウトライン生成(1記事1回)
tags: [cgmp, outline]
model: chat-standard
includes: [no_hallucination]
variables:
  type: object
  required: [target, topic, intent, seo_intent, platform, evidence, template_name,
             skeleton, title_count, title_max_len, max_sections, ai_smell_phrases]
  properties:
    target:            {type: string}
    topic:             {type: string}
    intent:            {type: string}
    seo_intent:        {type: string}
    platform:          {type: string}
    title_count:       {type: integer}
    title_max_len:     {type: integer}
    max_sections:      {type: integer}
    # 以下は呼び出し側で組み立てる(テンプレートに制御構文を持たせないため)
    evidence:          {type: string}
    template_name:     {type: string}
    skeleton:          {type: string}
    ai_smell_phrases:  {type: string}
---
あなたは{{ target }}向けの記事構成を設計する編集者です。

記事テーマ: {{ topic }}
検索意図(独自分類): {{ intent }}
SEO意図: {{ seo_intent }}
想定読者: {{ target }}
掲載媒体: {{ platform }}
DDE由来の根拠情報(競合上位URL・関連キーワード):
{{ evidence }}

この記事は「{{ template_name }}」型です。以下の骨格に従ってください。骨格の順序と役割は
変更せず、見出しの具体的な文言と論点の肉付けだけを行ってください。

骨格:
{{ skeleton }}

制約:
- タイトル案をちょうど{{ title_count }}件出す。各案は{{ title_max_len }}字以内を目安とし、主要キーワード
  「{{ topic }}」を含める
- **H2見出しは{{ max_sections }}本以内(厳守)。** 骨格1項目につきH2は1本までとし、
  骨格を複数のH2に分割しない。詳細は各H2の論点(points)かH3として表現する
- 各H2に論点(points)を2〜4件付ける
- H3は必要な場合のみ、各H2の論点として記述する(構造としては出力しない)
- FAQ候補を3件、CTA候補を2件出す
- keywords に、この記事の索引語になる名詞を3〜5件出す(記事分類用のタグに使う)
- 次の定型表現は使わない: {{ ai_smell_phrases }}
{{ include.no_hallucination }}

以下のJSONだけを出力してください。前置き・後書き・コードフェンスは不要です。

{
  "titles": [
    {"title": "タイトル案1", "reason": "根拠"},
    {"title": "タイトル案2", "reason": "根拠"},
    {"title": "タイトル案3", "reason": "根拠"}
  ],
  "structure": [
    {"level": "H2", "heading": "見出し文言", "points": ["論点1", "論点2"]}
  ],
  "faq": [{"question": "質問", "answer_hint": "回答の要点"}],
  "cta": ["CTA候補1", "CTA候補2"],
  "keywords": ["索引語1", "索引語2", "索引語3"]
}
