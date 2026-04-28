#!/usr/bin/env python3
"""
Translate German questions in Strapi into the other enabled locales (es, fr, it, pl)
using the DeepL API, then PUT each translation back so each documentId has a version
in every language.

Non-destructive: only adds locale versions to existing documents; never deletes.
Resumable: by default skips (doc, locale) pairs that already exist.
"""

import argparse
import os
import sys
import time
from pathlib import Path

import requests

SOURCE_LOCALE = 'de'
DEFAULT_TARGETS = ['es', 'fr', 'it', 'pl']
LOCALIZED_FIELDS = ['question', 'choice_1', 'choice_2', 'choice_3', 'choice_4', 'choice_5']

# Strapi locale code -> DeepL target_lang code
DEEPL_LANG = {'de': 'DE', 'es': 'ES', 'fr': 'FR', 'it': 'IT', 'pl': 'PL'}

REPO_ROOT = Path(__file__).resolve().parent.parent
ENV_FILES = [REPO_ROOT / '.env', REPO_ROOT / 'scripts' / '.env']

PAGE_SIZE = 100
HTTP_TIMEOUT = 120
DEEPL_BATCH = 30  # strings per /v2/translate call


class Abort(Exception):
    pass


def load_dotenv(paths):
    for path in paths:
        if not path.is_file():
            continue
        for raw in path.read_text(encoding='utf-8').splitlines():
            line = raw.strip()
            if not line or line.startswith('#') or '=' not in line:
                continue
            key, _, value = line.partition('=')
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def env_or_die(name):
    v = os.environ.get(name)
    if not v:
        raise Abort(f'Environment variable {name} is required but not set.')
    return v


def strapi_session(token):
    s = requests.Session()
    s.headers.update({'Authorization': f'Bearer {token}', 'Accept': 'application/json'})
    return s


def deepl_session(key):
    s = requests.Session()
    s.headers.update({
        'Authorization': f'DeepL-Auth-Key {key}',
        'Content-Type': 'application/json',
        'Accept': 'application/json',
    })
    return s


def deepl_base(key):
    return 'https://api-free.deepl.com' if key.endswith(':fx') else 'https://api.deepl.com'


def fetch_all_questions(session, base_url):
    out = []
    page = 1
    while True:
        r = session.get(
            f'{base_url}/api/questions',
            params={
                'locale': '*',
                'pagination[page]': page,
                'pagination[pageSize]': PAGE_SIZE,
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


def group_by_document(entries):
    grouped = {}
    for e in entries:
        doc_id = e.get('documentId')
        loc = e.get('locale')
        if not doc_id or not loc:
            continue
        grouped.setdefault(doc_id, {})[loc] = e
    return grouped


def localized_payload(entry):
    out = {}
    for f in LOCALIZED_FIELDS:
        v = entry.get(f)
        if isinstance(v, str) and v.strip():
            out[f] = v
    return out


def deepl_usage(session, base):
    r = session.get(f'{base}/v2/usage', timeout=HTTP_TIMEOUT)
    if r.status_code != 200:
        raise Abort(f'GET /v2/usage failed: {r.status_code} {r.text[:300]}')
    body = r.json()
    return body.get('character_count', 0), body.get('character_limit', 0)


def deepl_translate(session, base, texts, target, formality):
    """Translate a list of strings DE -> target. Returns the list of translations.

    Handles 429 (backoff), 456 (quota -> raise Abort), and 400 formality unsupported.
    """
    payload = {
        'text': texts,
        'source_lang': 'DE',
        'target_lang': target,
    }
    if formality:
        payload['formality'] = formality

    delay = 1
    for attempt in range(4):
        r = session.post(f'{base}/v2/translate', json=payload, timeout=HTTP_TIMEOUT)
        if r.status_code == 200:
            return [t['text'] for t in r.json()['translations']]
        if r.status_code == 429:
            if attempt == 3:
                raise Abort(f'DeepL 429 rate-limited after retries; last body: {r.text[:300]}')
            time.sleep(delay)
            delay *= 2
            continue
        if r.status_code == 456:
            raise Abort('DeepL quota exhausted (456). Re-run later (the script is resumable).')
        if r.status_code == 400 and 'formality' in r.text.lower() and 'formality' in payload:
            payload.pop('formality')
            continue
        raise Abort(f'DeepL /v2/translate failed: {r.status_code} {r.text[:500]}')
    raise Abort('DeepL retry loop fell through')


def chunks(seq, n):
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


def confirm(prompt, expected='TRANSLATE'):
    try:
        return input(prompt).strip() == expected
    except EOFError:
        return False


def main():
    parser = argparse.ArgumentParser(description='Translate German questions to other locales via DeepL.')
    parser.add_argument('--locales', default=','.join(DEFAULT_TARGETS),
                        help='Comma-separated target locales (Strapi codes). Default: es,fr,it,pl')
    parser.add_argument('--limit', type=int, default=None, help='Only process the first N source docs.')
    parser.add_argument('--force', action='store_true',
                        help='Translate and PUT even when the target locale version already exists.')
    parser.add_argument('--cost-estimate', action='store_true',
                        help='Discovery + char counts + DeepL usage. No DeepL calls, no PUTs.')
    parser.add_argument('--dry-run', action='store_true',
                        help='Discovery + cost estimate only. No DeepL calls, no PUTs.')
    parser.add_argument('--formality', default='more', choices=['more', 'less', 'default'],
                        help='DeepL formality. "default" omits the param. Default: more.')
    parser.add_argument('--yes', action='store_true', help='Skip the TRANSLATE confirmation prompt.')
    args = parser.parse_args()

    load_dotenv(ENV_FILES)
    try:
        strapi_url = env_or_die('STRAPI_URL').rstrip('/')
        strapi_token = env_or_die('STRAPI_EMAIL_TOKEN')
        deepl_key = env_or_die('DEEPL_API_KEY')
    except Abort as exc:
        print(f'ERROR: {exc}', file=sys.stderr)
        return 2

    targets = [t.strip() for t in args.locales.split(',') if t.strip()]
    unknown = [t for t in targets if t not in DEEPL_LANG]
    if unknown:
        print(f'ERROR: unknown target locale(s): {unknown}. Allowed: {sorted(DEEPL_LANG)}', file=sys.stderr)
        return 2
    if SOURCE_LOCALE in targets:
        print(f'ERROR: source locale {SOURCE_LOCALE} cannot be a target.', file=sys.stderr)
        return 2

    formality = None if args.formality == 'default' else args.formality

    s_strapi = strapi_session(strapi_token)
    s_deepl = deepl_session(deepl_key)
    deepl_url = deepl_base(deepl_key)
    deepl_tier = 'free' if deepl_url.endswith('api-free.deepl.com') else 'pro'

    print(f'Strapi:   {strapi_url}')
    print(f'DeepL:    {deepl_url}  ({deepl_tier} tier)')

    # 1. Discovery
    try:
        entries = fetch_all_questions(s_strapi, strapi_url)
    except Abort as exc:
        print(f'ERROR: {exc}', file=sys.stderr)
        return 2

    grouped = group_by_document(entries)
    sources = [(doc_id, by_locale[SOURCE_LOCALE]) for doc_id, by_locale in grouped.items()
               if SOURCE_LOCALE in by_locale]
    sources.sort(key=lambda x: x[1].get('id', 0))
    if args.limit is not None:
        sources = sources[:args.limit]

    print(f'Source:   {len(sources)} questions in locale={SOURCE_LOCALE}')

    # Build the work plan
    plan = {t: [] for t in targets}  # locale -> list of (documentId, payload_dict)
    for doc_id, src in sources:
        existing = set(grouped[doc_id].keys())
        payload = localized_payload(src)
        if not payload:
            continue
        for t in targets:
            if t in existing and not args.force:
                continue
            plan[t].append((doc_id, payload))

    # 2. Cost estimate
    chars_per_locale = {}
    for t, items in plan.items():
        chars_per_locale[t] = sum(len(v) for _, payload in items for v in payload.values())
    total_chars = sum(chars_per_locale.values())

    print('Plan:')
    for t in targets:
        print(f'  {t}: {len(plan[t])} docs, {chars_per_locale[t]:,} chars')
    print(f'  total: {sum(len(plan[t]) for t in targets)} doc-locale pairs, {total_chars:,} chars')

    try:
        used, limit = deepl_usage(s_deepl, deepl_url)
        remaining = max(0, limit - used)
        print(f'DeepL quota: {used:,} / {limit:,} used  ({remaining:,} remaining)')
        if total_chars > remaining:
            print(f'WARNING: this run needs {total_chars:,} chars but only {remaining:,} are left.')
            print('         Either upgrade to Pro, or run --locales <one> at a time as the quota allows.')
    except Abort as exc:
        print(f'ERROR: {exc}', file=sys.stderr)
        return 2

    if args.cost_estimate:
        return 0
    if args.dry_run:
        print('Dry-run complete. No DeepL calls, no PUTs.')
        return 0

    if total_chars == 0:
        print('Nothing to translate (every target locale is already covered for these docs). Exiting.')
        return 0

    # 3. Confirmation
    if not args.yes and not confirm('Type TRANSLATE to proceed: '):
        print('Aborted by operator.')
        return 1

    # 4. Translate + PUT, per target locale
    grand_ok = 0
    grand_failed = []
    for t in targets:
        items = plan[t]
        if not items:
            print(f'[{t}] nothing to do')
            continue

        print(f'[{t}] translating {len(items)} docs ({chars_per_locale[t]:,} chars)...')

        # Flatten into ordered list of strings, with index map back to (doc_idx, field)
        flat_strings = []
        index_map = []  # parallel to flat_strings
        for doc_idx, (_, payload) in enumerate(items):
            for field in LOCALIZED_FIELDS:
                if field in payload:
                    index_map.append((doc_idx, field))
                    flat_strings.append(payload[field])

        translated = []
        try:
            for i, batch in enumerate(chunks(flat_strings, DEEPL_BATCH), start=1):
                t_batch = deepl_translate(s_deepl, deepl_url, batch, DEEPL_LANG[t], formality)
                translated.extend(t_batch)
                done = min(i * DEEPL_BATCH, len(flat_strings))
                if i % 5 == 0 or done == len(flat_strings):
                    print(f'  ...translated {done}/{len(flat_strings)} strings')
        except Abort as exc:
            print(f'[{t}] DeepL aborted: {exc}', file=sys.stderr)
            print(f'[{t}] no PUTs sent for this locale.', file=sys.stderr)
            grand_failed.append((t, str(exc)))
            continue

        # Reassemble per-doc payloads
        per_doc = [{} for _ in items]
        for (doc_idx, field), text in zip(index_map, translated):
            per_doc[doc_idx][field] = text

        ok = 0
        for (doc_id, _), translated_payload in zip(items, per_doc):
            r = s_strapi.put(
                f'{strapi_url}/api/questions/{doc_id}',
                params={'locale': t},
                json={'data': translated_payload},
                timeout=HTTP_TIMEOUT,
            )
            if r.status_code in (200, 201):
                ok += 1
                if ok % 25 == 0:
                    print(f'  ...pushed {ok}/{len(items)} to {t}')
            else:
                grand_failed.append((t, f'{doc_id}: {r.status_code} {r.text[:200]}'))

        print(f'[{t}] pushed {ok} / {len(items)} docs')
        grand_ok += ok

    print(f'Done. {grand_ok} doc-locale pairs translated and pushed; {len(grand_failed)} failures.')
    if grand_failed:
        for locale, msg in grand_failed:
            print(f'  {locale}: {msg}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
