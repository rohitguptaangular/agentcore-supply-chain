/* Example of the file `make frontend` generates as config.js.
 *
 * config.js is written from CloudFormation stack outputs at publish time and
 * is not committed — it contains the API endpoint of whichever account the
 * stack was deployed to. This example exists so the shape is documented.
 */
window.APP_CONFIG = {
  apiEndpoint: "https://abc123xyz.execute-api.us-east-1.amazonaws.com",

  // Mirrors what was actually deployed, so the sidebar reports reality rather
  // than what the UI assumes exists.
  features: {
    oauth: true,
    gateway: true,
    knowledgeBase: true,
    memory: true,
    guardrails: true,
    vpc: false,
  },
};
