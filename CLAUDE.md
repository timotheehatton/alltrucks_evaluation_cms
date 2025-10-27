# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

This is a Strapi CMS application for managing truck technician training and evaluation content. The application provides a headless CMS backend that serves content for a technician assessment platform.

## Requirements

- Node.js: >=18.0.0 <=20.x.x
- npm: >=6.0.0

## Commands

### Development
- `npm run develop` - Start Strapi with autoReload enabled for development
- `npm run start` - Start Strapi in production mode (autoReload disabled)
- `npm run build` - Build the admin panel
- `npm run deploy` - Deploy to Strapi Cloud

### Strapi CLI
- `npm run strapi` - Run Strapi CLI commands directly

## Architecture

### Content Types

The application is organized around several main content types in `src/api/`:

1. **Training** (`training`) - CollectionType representing training modules
   - Categories: general_mechanic, powertrain, electricity, diagnostic, engine_exhaust, engine_injection, truck_air_braking_system, trailer_braking_system
   - Includes training title, description, category, number, and maximum score

2. **Question** (`question`) - CollectionType representing evaluation questions
   - Supports up to 5 multiple choice options
   - Includes optional image attachments
   - Categorized by the same training categories
   - Stores correct answer as enumeration

3. **Test** (`test`) - SingleType representing the evaluation page UI content
   - Localized content for test interface
   - Success and timeout popups configuration

4. **User Statistic** (`user-statistic`) - CollectionType for tracking user performance

5. **Workshop Technician** (`workshop-technician`) - CollectionType for technician profiles

6. **Category** (`category`) - CollectionType for organizing content

7. **Email** (`email`) - SingleType for email template content

8. **Menu** (`menu`) - SingleType for navigation configuration

9. **Account** (`account`) - SingleType for account page UI content (user profile, workshop info, password change)

10. **Login** (`login`) - SingleType for login page and password reset UI content

11. **Activate Account** (`activate-account`) - SingleType for account activation and password reset page UI

12. **Global PDF** (`global-pdf`) - SingleType for PDF template content (diploma generation)

### Project Structure

```
src/
├── api/                    # Content types, controllers, services, and routes
│   ├── [content-type]/
│   │   ├── content-types/  # Schema definitions (JSON)
│   │   ├── controllers/    # Request handlers
│   │   ├── routes/         # API route definitions
│   │   └── services/       # Business logic
├── admin/                  # Admin panel customizations
├── extensions/             # Plugin extensions
└── index.js                # Application entry point

config/
├── admin.js                # Admin panel configuration
├── api.js                  # API configuration
├── database.js             # Database configuration
├── middlewares.js          # Middleware configuration
├── plugins.js              # Plugin configuration (SendGrid email)
└── server.js               # Server configuration
```

### Key Features

- **i18n Support**: All content types use the i18n plugin with localized content
- **Email Integration**: SendGrid email provider configured in `config/plugins.js`
- **Import/Export**: `strapi-plugin-import-export-entries` plugin for bulk content management
- **Database**: Uses better-sqlite3 for development

### Environment Variables

Required variables (see `.env.example`):
- `HOST`, `PORT` - Server configuration
- `APP_KEYS`, `API_TOKEN_SALT`, `ADMIN_JWT_SECRET`, `TRANSFER_TOKEN_SALT`, `JWT_SECRET` - Security tokens
- `SENDGRID_API_KEY`, `SENDGRID_DEFAULT_FROM`, `SENDGRID_DEFAULT_TO` - Email configuration

### Content Type Conventions

- Most content types follow Strapi factory pattern: `createCoreController`, `createCoreService`, `createCoreRouter`
- Schema files define the data model with pluginOptions for i18n localization
- Controllers, services, and routes are organized in separate directories per content type
- SingleTypes (like `test`, `email`, `menu`) represent one-off configuration content
- CollectionTypes (like `training`, `question`) represent repeatable content