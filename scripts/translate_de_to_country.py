#!/usr/bin/env python3
"""
Translate the German question pool into every target country and create
new questions in Strapi with `country=<target>`.

Built for the post-i18n schema where Question has a `country` enum
instead of localized fields. Replaces the older `translate_questions.py`
which assumed each documentId had one entry per locale.

Workflow:
  1. Fetch every question currently in Strapi with country=DE
  2. For each target country (default: ES, IT, PL — FR is uploaded
     separately from the April backup), translate the text fields via
     DeepL in batches
  3. POST each translation as a new question with the target country

Resumable: skips countries that already have at least one question
(use --force-country=XX to re-translate).

Reuses the DeepL/strapi helpers in translate_questions.py.
"""

import argparse
import os
import sys
import time
from pathlib import Path

import requests

# Same helpers as translate_questions.py — we reuse the file directly so
# DeepL retry/quota handling stays consistent.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from translate_questions import (  # noqa: E402
    Abort, load_dotenv, env_or_die, strapi_session, deepl_session, deepl_base,
    deepl_translate, deepl_usage, chunks, ENV_FILES,
)

PAGE_SIZE = 100
HTTP_TIMEOUT = 120
SOURCE_COUNTRY = 'DE'
DEEPL_LANG = {'ES': 'ES', 'FR': 'FR', 'IT': 'IT', 'PL': 'PL'}
DEFAULT_TARGETS = ['ES', 'IT', 'PL']
TRANSLATABLE_FIELDS = ['question', 'choice_1', 'choice_2', 'choice_3', 'choice_4', 'choice_5']


def fetch_country_docs(session, base_url, country):
    out = []
    page = 1
    while True:
        r = session.get(
            f'{base_url}/api/questions',
            params={
                'filters[country][$eq]': country,
                'pagination[page]': page,
                'pagination[pageSize]': PAGE_SIZE,
                'populate': 'image',
            },
            timeout=HTTP_TIMEOUT,
        )
        if r.status_code != 200:
            raise Abort(f'GET /api/questions page={page} failed: {r.status_code} {r.text[:500]}')
        body = r.json()
        out.extend(body.get('data', []))
        pagination = body.get('meta', {}).get('pagination', {})
        if page >= pagination.get('pageCount', 1):
            break
        page += 1
    return out


def count_docs(session, base_url, country):
    r = session.get(
        f'{base_url}/api/questions',
        params={
            'filters[country][$eq]': country,
            'pagination[pageSize]': 1,
        },
        timeout=HTTP_TIMEOUT,
    )
    if r.status_code != 200:
        raise Abort(f'GET /api/questions failed: {r.status_code} {r.text[:300]}')
    return r.json().get('meta', {}).get('pagination', {}).get('total', 0)


def collect_translatable_texts(docs):
    """Return (flat_strings, per_doc_index). Each per_doc_index entry maps a
    field name → position in flat_strings, so we can rebuild payloads after
    translation."""
    flat = []
    per_doc = []
    for d in docs:
        idx = {}
        for f in TRANSLATABLE_FIELDS:
            v = d.get(f)
            if isinstance(v, str) and v.strip():
                idx[f] = len(flat)
                flat.append(v)
        per_doc.append(idx)
    return flat, per_doc


def build_payload(source_doc, translated_per_field, country):
    payload = {
        'category': source_doc.get('category'),
        'anwser': source_doc.get('anwser'),
        'difficulty': source_doc.get('difficulty'),
        'country': country,
    }
    for f in TRANSLATABLE_FIELDS:
        if f in translated_per_field:
            payload[f] = translated_per_field[f]
    img = source_doc.get('image')
    if img and isinstance(img, dict) and img.get('id'):
        payload['image'] = img['id']
    return payload


def confirm(prompt, expected='TRANSLATE'):
    try:
        return input(prompt).strip() == expected
    except EOFError:
        return False


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        '--countries',
        default=','.join(DEFAULT_TARGETS),
        help='Comma-separated target country codes (FR/ES/IT/PL). Default: ES,IT,PL',
    )
    parser.add_argument('--limit', type=int, default=None, help='Only process the first N DE docs.')
    parser.add_argument('--force-country', action='append', default=[],
                        help='Country code that should be re-translated even if it already has questions. Repeatable.')
    parser.add_argument('--cost-estimate', action='store_true',
                        help='Print DeepL char count + usage. No DeepL calls, no POSTs.')
    parser.add_argument('--dry-run', action='store_true', help='Discover + count. No DeepL calls, no POSTs.')
    parser.add_argument('--formality', default='more', choices=['more', 'less', 'default'])
    parser.add_argument('--yes', action='store_true', help='Skip the TRANSLATE confirmation prompt.')
    parser.add_argument('--batch', type=int, default=30, help='DeepL strings per request.')
    args = parser.parse_args()

    load_dotenv(ENV_FILES)
    try:
        strapi_url = env_or_die('STRAPI_URL').rstrip('/')
        strapi_token = env_or_die('STRAPI_EMAIL_TOKEN')
        deepl_key = env_or_die('DEEPL_API_KEY')
    except Abort as exc:
        print(f'ERROR: {exc}', file=sys.stderr)
        return 2

    targets = [c.strip().upper() for c in args.countries.split(',') if c.strip()]
    unknown = [c for c in targets if c not in DEEPL_LANG]
    if unknown:
        print(f'ERROR: unknown target country code(s): {unknown}. Allowed: {sorted(DEEPL_LANG)}', file=sys.stderr)
        return 2

    formality = None if args.formality == 'default' else args.formality
    force = {c.upper() for c in args.force_country}

    s_strapi = strapi_session(strapi_token)
    s_deepl = deepl_session(deepl_key)
    deepl_url = deepl_base(deepl_key)
    deepl_tier = 'free' if deepl_url.endswith('api-free.deepl.com') else 'pro'

    print(f'Strapi:   {strapi_url}')
    print(f'DeepL:    {deepl_url}  ({deepl_tier} tier)')

    # 1. Discovery — fetch DE source
    de_docs = fetch_country_docs(s_strapi, strapi_url, SOURCE_COUNTRY)
    if args.limit:
        de_docs = de_docs[:args.limit]
    print(f'Source:   {len(de_docs)} questions with country=DE')

    # 2. For each target country, check if it already has data
    todo = []
    for c in targets:
        existing = count_docs(s_strapi, strapi_url, c)
        if existing > 0 and c not in force:
            print(f'Skip:     country={c} already has {existing} questions (pass --force-country={c} to re-translate)')
            continue
        todo.append(c)
    if not todo:
        print('Nothing to do. Pass --force-country to re-translate.')
        return 0

    flat_texts, per_doc_index = collect_translatable_texts(de_docs)
    total_chars = sum(len(t) for t in flat_texts)
    print(f'Will translate {len(flat_texts)} strings ({total_chars:,} chars) for each of {todo}.')
    print(f'Total DeepL cost: ~{total_chars * len(todo):,} chars across {len(todo)} languages.')

    # 3. DeepL usage check
    try:
        used, limit = deepl_usage(s_deepl, deepl_url)
        print(f'DeepL usage: {used:,} / {limit:,} chars')
        if used + total_chars * len(todo) > limit:
            print(f'WARNING: estimated translation exceeds remaining DeepL quota.', file=sys.stderr)
    except Abort as exc:
        print(f'WARNING: could not fetch DeepL usage: {exc}', file=sys.stderr)

    if args.cost_estimate or args.dry_run:
        return 0

    if not args.yes and not confirm('Type TRANSLATE to proceed: '):
        print('Aborted by operator.')
        return 1

    # 4. Translate + POST
    for country in todo:
        print(f'\n=== Translating + posting for country={country} ===')
        deepl_target = DEEPL_LANG[country]

        translated_flat = []
        for batch in chunks(flat_texts, args.batch):
            translated_flat.extend(deepl_translate(s_deepl, deepl_url, batch, deepl_target, formality))
            print(f'  DeepL: {len(translated_flat)}/{len(flat_texts)} strings translated')

        ok = 0
        failures = []
        for source_doc, idx_map in zip(de_docs, per_doc_index):
            translated_fields = {f: translated_flat[pos] for f, pos in idx_map.items()}
            payload = build_payload(source_doc, translated_fields, country)
            r = s_strapi.post(f'{strapi_url}/api/questions', json={'data': payload}, timeout=HTTP_TIMEOUT)
            if r.status_code not in (200, 201):
                failures.append(f'doc id={source_doc.get("id")}: {r.status_code} {r.text[:200]}')
                continue
            ok += 1
            if ok % 25 == 0:
                print(f'  POST:  {ok}/{len(de_docs)}')
        print(f'  {country}: {ok} / {len(de_docs)} posted ({len(failures)} failed)')
        for msg in failures[:5]:
            print(f'    {msg}', file=sys.stderr)

    return 0


if __name__ == '__main__':
    sys.exit(main())