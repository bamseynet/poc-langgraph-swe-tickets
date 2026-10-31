import { Arn, Stack } from "aws-cdk-lib";
import { Effect, PolicyStatement } from "aws-cdk-lib/aws-iam";

const INVOKE_ACTIONS = ["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"];

/** Strip the geographic prefix from a cross-region inference profile id. */
export function foundationModelId(profileId: string): string {
  return profileId.replace(/^(us-gov|us|eu|apac|au|ca|jp|sa|amer|global)\./, "");
}

/**
 * Allow invoking these inference profiles, and nothing else.
 *
 * AWS requires access to the foundation model in every Region a profile can route to. The
 * Region is a wildcard because those destinations depend on the source Region, and the
 * condition limits the wildcard to requests made through these profiles.
 * See https://docs.aws.amazon.com/bedrock/latest/userguide/inference-profiles-prereq.html
 */
export function bedrockStatements(stack: Stack, profileIds: string[]): PolicyStatement[] {
  const unique = [...new Set(profileIds)];
  const profileArns = unique.map((id) =>
    Arn.format({ service: "bedrock", resource: "inference-profile", resourceName: id }, stack),
  );
  const modelArns = unique.map(
    (id) => `arn:${stack.partition}:bedrock:*::foundation-model/${foundationModelId(id)}`,
  );
  return [
    new PolicyStatement({
      effect: Effect.ALLOW,
      actions: [...INVOKE_ACTIONS, "bedrock:GetInferenceProfile"],
      resources: profileArns,
    }),
    new PolicyStatement({
      effect: Effect.ALLOW,
      actions: INVOKE_ACTIONS,
      resources: modelArns,
      conditions: { StringEquals: { "bedrock:InferenceProfileArn": profileArns } },
    }),
  ];
}
