'use strict';

/**
 * workshop-technician service
 */

const { createCoreService } = require('@strapi/strapi').factories;

module.exports = createCoreService('api::workshop-technician.workshop-technician');
