module.exports = ({ env }) => ({
    email: {
        config: {
            provider: 'sendgrid',
            providerOptions: {
                apiKey: env('SENDGRID_API_KEY'),
            },
            settings: {
                defaultFrom: 'support@alltrucks-fleet-platform.com',
                defaultReplyTo: 'support@alltrucks-fleet-platform.com',
            },
        },
    },
});