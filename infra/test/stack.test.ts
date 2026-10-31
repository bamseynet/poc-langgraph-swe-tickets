import { App } from "aws-cdk-lib";
import { Match, Template } from "aws-cdk-lib/assertions";
import { describe, expect, test } from "vitest";
import { foundationModelId } from "../lib/bedrock-policy.js";
import { TicketgateStack, type TicketgateProps } from "../lib/ticketgate-stack.js";

const SONNET = "us.anthropic.claude-sonnet-5-5";
const HAIKU = "us.anthropic.claude-haiku-5-5";

function synth(overrides: Partial<TicketgateProps> = {}): Template {
  const app = new App();
  const stack = new TicketgateStack(app, "Test", {
    env: { account: "111122223333", region: "us-east-1" },
    modelId: SONNET,
    workerModelId: HAIKU,
    githubRepo: "octo-org/octo-repo",
    targetRepoUrl: "https://github.com/octo-org/octo-repo.git",
    allowedCidr: "203.0.113.0/24",
    ...overrides,
  });
  return Template.fromStack(stack);
}

function policyStatements(template: Template): Record<string, unknown>[] {
  const policies = template.findResources("AWS::IAM::Policy");
  return Object.values(policies).flatMap(
    (policy) => policy.Properties.PolicyDocument.Statement as Record<string, unknown>[],
  );
}

function bedrockStatements(template: Template) {
  return policyStatements(template).filter((statement) =>
    [statement.Action].flat().some((action) => String(action).startsWith("bedrock:")),
  );
}

describe("foundationModelId", () => {
  test.each([
    ["us.anthropic.claude-sonnet-5-5", "anthropic.claude-sonnet-5-5"],
    ["eu.anthropic.claude-haiku-5-5", "anthropic.claude-haiku-5-5"],
    ["global.anthropic.claude-opus-5-5", "anthropic.claude-opus-5-5"],
    ["us-gov.anthropic.claude-haiku-5-5", "anthropic.claude-haiku-5-5"],
    ["anthropic.claude-opus-5-5", "anthropic.claude-opus-5-5"],
  ])("%s becomes %s", (profile, expected) => {
    expect(foundationModelId(profile)).toBe(expected);
  });
});

describe("least privilege", () => {
  const template = synth();

  test("Bedrock access names the two models and no others", () => {
    const resources = JSON.stringify(bedrockStatements(template).map((s) => s.Resource));
    expect(resources).toContain("inference-profile/us.anthropic.claude-sonnet-5-5");
    expect(resources).toContain("inference-profile/us.anthropic.claude-haiku-5-5");
    expect(resources).toContain("foundation-model/anthropic.claude-sonnet-5-5");
    expect(resources).toContain("foundation-model/anthropic.claude-haiku-5-5");
    expect(resources).not.toContain("foundation-model/*");
    expect(resources).not.toContain("inference-profile/*");
  });

  test("foundation models can only be reached through the inference profiles", () => {
    const statements = bedrockStatements(template).filter((s) =>
      JSON.stringify(s.Resource).includes("foundation-model/"),
    );
    expect(statements.length).toBeGreaterThan(0);
    for (const statement of statements) {
      expect(JSON.stringify(statement.Condition)).toContain("bedrock:InferenceProfileArn");
    }
  });

  test("the task can invoke models but not administer Bedrock", () => {
    const actions = bedrockStatements(template).flatMap((s) => [s.Action].flat());
    expect(new Set(actions)).toEqual(
      new Set([
        "bedrock:InvokeModel",
        "bedrock:InvokeModelWithResponseStream",
        "bedrock:GetInferenceProfile",
      ]),
    );
  });

  test("no policy allows every action", () => {
    for (const statement of policyStatements(template)) {
      for (const action of [statement.Action].flat()) {
        expect(String(action)).not.toBe("*");
        expect(String(action)).not.toMatch(/^[a-z-]+:\*$/);
      }
    }
  });
});

describe("secrets", () => {
  const template = synth();

  test("tokens reach the container as secret references, not plain environment", () => {
    const task = Object.values(template.findResources("AWS::ECS::TaskDefinition"))[0];
    const container = task?.Properties.ContainerDefinitions[0];
    const plain = container.Environment.map((e: { Name: string }) => e.Name);
    const secret = container.Secrets.map((e: { Name: string }) => e.Name);
    expect(secret).toEqual(expect.arrayContaining(["GITHUB_TOKEN", "REVIEWER_TOKENS"]));
    expect(plain).not.toContain("GITHUB_TOKEN");
    expect(plain).not.toContain("REVIEWER_TOKENS");
  });

  test("no secret value is written into the template", () => {
    const secrets = Object.values(template.findResources("AWS::SecretsManager::Secret"));
    expect(secrets.length).toBe(2);
    for (const secret of secrets) {
      expect(secret.Properties.SecretString).toBeUndefined();
    }
  });
});

describe("first deployment", () => {
  test("the generated reviewer secret is valid config, so the task can start", () => {
    // A plain random string would make the API refuse to start and roll the stack back.
    synth().hasResourceProperties("AWS::SecretsManager::Secret", {
      GenerateSecretString: Match.objectLike({
        SecretStringTemplate: "{}",
        GenerateStringKey: "admin",
      }),
    });
  });
});

describe("runtime", () => {
  const template = synth();

  test("configures the real model, source and SQLite on a network file system", () => {
    const task = Object.values(template.findResources("AWS::ECS::TaskDefinition"))[0];
    const env = Object.fromEntries(
      task?.Properties.ContainerDefinitions[0].Environment.map(
        (e: { Name: string; Value: string }) => [e.Name, e.Value],
      ),
    );
    expect(env).toMatchObject({
      LLM_MODE: "bedrock",
      BEDROCK_MODEL_ID: SONNET,
      BEDROCK_WORKER_MODEL_ID: HAIKU,
      AWS_REGION: "us-east-1",
      TICKET_SOURCE: "github",
      GITHUB_REPO: "octo-org/octo-repo",
      DATA_DIR: "/data",
      // SQLite's WAL mode does not work on a network file system.
      SQLITE_JOURNAL_MODE: "DELETE",
      // A stopped task's lock can take a while to expire on a network file system.
      DATA_LOCK_WAIT_SECONDS: "120",
    });
  });

  test("never runs two tasks on one SQLite file, even during a deployment", () => {
    template.hasResourceProperties("AWS::ECS::Service", {
      DesiredCount: 1,
      DeploymentConfiguration: Match.objectLike({
        MaximumPercent: 100,
        MinimumHealthyPercent: 0,
      }),
    });
  });

  test("data outlives the stack and is encrypted", () => {
    template.hasResource("AWS::EFS::FileSystem", {
      DeletionPolicy: "Retain",
      Properties: Match.objectLike({ Encrypted: true }),
    });
  });

  test("health checks use the unauthenticated endpoint", () => {
    template.hasResourceProperties("AWS::ElasticLoadBalancingV2::TargetGroup", {
      HealthCheckPath: "/api/health",
    });
  });
});

describe("network exposure", () => {
  function albIngress(template: Template) {
    const groups = template.findResources("AWS::EC2::SecurityGroup");
    return Object.values(groups).flatMap((g) => g.Properties.SecurityGroupIngress ?? []);
  }

  test("is limited to the allowed CIDR", () => {
    const cidrs = albIngress(synth()).map((rule: { CidrIp?: string }) => rule.CidrIp);
    expect(cidrs).toContain("203.0.113.0/24");
    expect(cidrs).not.toContain("0.0.0.0/0");
  });

  test("is closed when no CIDR is given", () => {
    const cidrs = albIngress(synth({ allowedCidr: undefined }))
      .map((rule: { CidrIp?: string }) => rule.CidrIp)
      .filter(Boolean);
    expect(cidrs).toEqual([]);
  });

  test("uses HTTPS when a certificate is supplied", () => {
    const template = synth({
      certificateArn: "arn:aws:acm:us-east-1:111122223333:certificate/abc",
    });
    template.hasResourceProperties("AWS::ElasticLoadBalancingV2::Listener", {
      Protocol: "HTTPS",
      Port: 443,
    });
  });

  test("the task accepts traffic only from the load balancer", () => {
    const template = synth();
    template.hasResourceProperties("AWS::ECS::Service", {
      NetworkConfiguration: Match.objectLike({
        AwsvpcConfiguration: Match.objectLike({ AssignPublicIp: "DISABLED" }),
      }),
    });
  });
});
