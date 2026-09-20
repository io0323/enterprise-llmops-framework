---
id: elf.smoke
version: 1
status: published
owner: io
description: ELF 自身の動作確認用。mock Adapter で完結し、実LLMを呼ばない
tags: [elf, smoke]
model: mock-echo
includes: [output_plain]
variables:
  type: object
  required: [message]
  properties:
    message: {type: string, minLength: 1}
---
次の内容をそのまま書き写してください。

{{ message }}

{{ include.output_plain }}
