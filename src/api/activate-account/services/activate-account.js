'use strict';

/**
 * activate-account service
 */

const { createCoreService } = require('@strapi/strapi').factories;

module.exports = createCoreService('api::activate-account.activate-account');
