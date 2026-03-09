# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Strapi v5.29.0 headless CMS backend for a truck technician training and evaluation platform. Serves content (trainings, questions, UI text) to a frontend assessment app.

## Requirements

- Node.js: >=18.0.0 <=22.x.x
- npm: >=6.0.0

## Commands

- `npm run develop` - Start Strapi with autoReload (development)
- `npm run start` - Start Strapi without autoReload (production)
- `npm run build` - Build the admin panel
- `npm run deploy` - Deploy to Strapi Cloud
- `npm run strapi` - Run Strapi CLI commands directly

## Architecture

### Content Types (`src/api/`)

**CollectionTypes** (repeatable content):
- **training** - Training modules with title, description, category, number, max score
- **question** - Multiple choice questions (up to 5 choices) with optional image; note: correct answer field is `anwser` (typo preserved in schema)
- **category** - Content categorization
- **user-statistic** - User performance tracking
- **workshop-technician** - Technician profiles

**SingleTypes** (one-off UI/config content):
- **test** - Evaluation page UI (success/timeout popups)
- **account** - Account page UI (profile, workshop, password sections)
- **login** - Login page and password reset UI
- **activate-account** - Account activation and password reset page
- **menu** - Navigation configuration
- **email** - Email template content
- **global-pdf** - PDF/diploma template content

**Shared category enum** (used by both `training` and `question`):
`general_mechanic`, `powertrain`, `electricity`, `diagnostic`, `engine_exhaust`, `engine_injection`, `truck_air_braking_system`, `trailer_braking_system`

### Key Patterns

- **All content types use default Strapi factory pattern** — no custom controllers, services, or routes. Every content type uses `createCoreController`, `createCoreService`, `createCoreRouter` with no overrides.
- **i18n enabled** on all content types via `pluginOptions.i18n.localized: true`
- **Draft & Publish disabled** on all content types (`draftAndPublish: false`)
- **No relations** between content types in schemas
- **No active admin customizations** — `src/admin/` only has example files
- **No active extensions** — `src/extensions/` is empty

### Configuration (`config/`)

- **database.js** - Supports SQLite (default), MySQL, PostgreSQL via env vars
- **plugins.js** - SendGrid email provider (`@strapi/provider-email-sendgrid`)
- **api.js** - REST defaults: limit 25, max 500, withCount enabled
- **middlewares.js** - Standard Strapi middleware stack

### Environment Variables

Required (see `.env.example`):
- `HOST`, `PORT` - Server config
- `APP_KEYS`, `API_TOKEN_SALT`, `ADMIN_JWT_SECRET`, `TRANSFER_TOKEN_SALT`, `JWT_SECRET` - Security tokens
- `SENDGRID_API_KEY`, `SENDGRID_DEFAULT_FROM`, `SENDGRID_DEFAULT_TO` - Email config

### Code Style

- ESLint configured: 2-space indent, single quotes, semicolons required, unix line endings
- JavaScript project (no TypeScript source; `types/generated/` contains auto-generated Strapi type definitions)