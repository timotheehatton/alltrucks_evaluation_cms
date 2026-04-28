'use strict';

/**
 * auto-reply-email service
 */

const { createCoreService } = require('@strapi/strapi').factories;

module.exports = createCoreService('api::auto-reply-email.auto-reply-email');