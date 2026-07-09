# Standard Source Research

## 1. Purpose

This note records whether the product can rely on internet search to read standard正文 when the local knowledge base is incomplete.

Test date: 2026-07-08.

## 2. Tested Sources

### GB/T 17766-2020 固体矿产资源储量分类

Official page:

```text
https://openstd.samr.gov.cn/bzgk/std/newGbInfo?hcno=3F98C03DE9AB232432B3732A491983E7
```

Observed result:

- The official page exposes metadata: standard number, Chinese title, English title, status, CCS, ICS, publication date, implementation date, issuing authority, and online preview entry.
- The online preview page exists, but the正文 is rendered through an image-based viewer.
- The fetched HTML contains page containers, `pdfImg-*` image slices, and `viewGbImg?fileName=...` image loading code.
- The standard正文 was not available as normal HTML text in the scraped page.

Classification:

```text
source_type: official_visual
text_access: image_ocr_required
answer_policy: metadata can be cited;正文 claims require local KB text or a validated OCR pipeline
```

### DZ/T 0321-2018 方解石矿地质勘查规范

SAMR official metadata page:

```text
https://std.samr.gov.cn/hb/search/stdHBDetailed?id=8B1827F254B3BB19E05397BE0A0AB44A
```

Observed result:

- The official page exposes metadata: standard title, standard number, publication date, implementation date, status,归口单位,主管部门,备案号, and related standards.
- The page shows the standard status as废止.
- No正文 or official full-text preview/download entry was found in the scraped page.

Classification:

```text
source_type: official_metadata
text_access: metadata_only
answer_policy: metadata can be cited;正文 claims must not be generated from this page alone
```

Natural Resources standard platform page:

```text
http://www.nrsis.org.cn/portal/stdDetail/211429
```

Observed result:

- The platform can find the standard through:

```text
http://www.nrsis.org.cn/portal/xxcx/std?pageNo=1&key=DZ%2FT%200321-2018&pageSize=20&pageOrderBy=&pageOrderType=
```

- The detail page exposes metadata: standard number, title, status label, publication date, implementation date, CCS, ICS, technical归口, drafting organizations, drafters, and remarks.
- The detail page provides a "点击查看标准全文" PDF icon.
- The full-text reader URL is:

```text
http://www.nrsis.org.cn/mnr_kfs/file/read/92643d1c54b76ad76499310212899902
```

- The reader page reports 37 pages and loads page content by POSTing to:

```text
http://www.nrsis.org.cn/mnr_kfs/file/readPage
```

with form fields:

```text
code=92643d1c54b76ad76499310212899902
page=1
```

- The `readPage` endpoint returns base64 PDF page data.
- Decoded pages are one-page PDF files, but `pdftotext` returns no正文 text. The PDF contains image objects, so the正文 appears to be image-based.
- The reader page says the electronic text is for personal learning/research and only for reference; formal publications prevail.
- There is a source discrepancy to investigate: the SAMR industry-standard page showed废止, while the NRSIS detail page showed a现行 label and a废止 timeline node without a date.

Classification:

```text
source_type: official_visual
text_access: image_ocr_required
answer_policy: official full-text access exists, but条款级 extraction requires OCR or visual parsing; status conflicts must be surfaced
```

## 3. Product Conclusion

Internet search can supplement the local knowledge base for official metadata, status checks, standard numbers, dates, and candidate source discovery. Some official platforms provide full-text readers, but they may still be image-based and not directly readable by a text-only LLM.

For standards-based answers, the product must distinguish:

- official metadata
- official readable full text
- official visual-only preview
- third-party candidate document
- unavailable text

If the local knowledge base lacks正文 and the internet source only provides metadata or image-only preview, the answer must say that the current evidence is insufficient for a条款级 conclusion unless an OCR/visual parsing pipeline has extracted and indexed the text.

## 4. Follow-up

An OCR pipeline may be evaluated for official visual-only previews, but this needs a separate legal and technical review before being used in production.
