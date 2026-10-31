import { Annotations, CfnOutput, Duration, RemovalPolicy, Stack, type StackProps } from "aws-cdk-lib";
import { Certificate } from "aws-cdk-lib/aws-certificatemanager";
import { Peer, Port, SecurityGroup, Vpc } from "aws-cdk-lib/aws-ec2";
import { Platform } from "aws-cdk-lib/aws-ecr-assets";
import {
  Cluster,
  ContainerImage,
  FargateService,
  FargateTaskDefinition,
  LogDrivers,
  Secret as EcsSecret,
} from "aws-cdk-lib/aws-ecs";
import { FileSystem } from "aws-cdk-lib/aws-efs";
import {
  ApplicationLoadBalancer,
  ApplicationProtocol,
  SslPolicy,
} from "aws-cdk-lib/aws-elasticloadbalancingv2";
import { LogGroup, RetentionDays } from "aws-cdk-lib/aws-logs";
import { Secret } from "aws-cdk-lib/aws-secretsmanager";
import { SecretValue } from "aws-cdk-lib";
import type { Construct } from "constructs";
import { fileURLToPath } from "node:url";
import { bedrockStatements } from "./bedrock-policy.js";

const CONTAINER_PORT = 8000;
const DATA_PATH = "/data";
const REPO_ROOT = fileURLToPath(new URL("../..", import.meta.url));
// Long enough to outlast the SSE keep-alive, so the progress stream stays open.
const ALB_IDLE_TIMEOUT = Duration.minutes(5);

export interface TicketgateProps extends StackProps {
  /** Inference profile id for the investigator. */
  modelId: string;
  /** Inference profile id for triage and drafted replies. */
  workerModelId: string;
  /** The owner/repo whose issues are processed. */
  githubRepo: string;
  /** Only ingest issues with this label. Recommended on a public repository. */
  githubLabelFilter?: string;
  /** The checkout the investigator reads, cloned when the container starts. */
  targetRepoUrl: string;
  /** Who may reach the load balancer. With no value, nobody can. */
  allowedCidr?: string;
  /** An ACM certificate for HTTPS. Without one, reviewer tokens cross the network in clear. */
  certificateArn?: string;
  /** The name the certificate covers. You point it at the load balancer yourself. */
  domainName?: string;
  pollIntervalSeconds?: number;
}

export class TicketgateStack extends Stack {
  constructor(scope: Construct, id: string, props: TicketgateProps) {
    super(scope, id, props);

    // A certificate can never cover the load balancer's own AWS hostname.
    if (props.certificateArn && !props.domainName) {
      throw new Error("certificateArn also needs domainName, the name the certificate covers.");
    }

    const vpc = new Vpc(this, "Vpc", { maxAzs: 2, natGateways: 1 });

    // SQLite needs a file that outlives the task. Retained because it holds the audit log.
    const fileSystem = new FileSystem(this, "Data", {
      vpc,
      encrypted: true,
      removalPolicy: RemovalPolicy.RETAIN,
    });
    const accessPoint = fileSystem.addAccessPoint("DataAccess", {
      path: "/ticketgate",
      createAcl: { ownerUid: "1000", ownerGid: "1000", permissions: "750" },
      posixUser: { uid: "1000", gid: "1000" },
    });

    // Their real values are set by hand after deployment and never appear here.
    const githubToken = new Secret(this, "GithubToken", {
      description: "GitHub token that can comment on and label issues in the target repository",
      removalPolicy: RemovalPolicy.RETAIN,
      // A placeholder, not a secret. Without it Secrets Manager invents a random value that
      // looks like a token, and every GitHub call fails with 401. The app refuses this value.
      secretStringValue: SecretValue.unsafePlainText("REPLACE_ME_AFTER_DEPLOY"),
    });
    const reviewerTokens = new Secret(this, "ReviewerTokens", {
      description: "JSON object of reviewer name to API token",
      removalPolicy: RemovalPolicy.RETAIN,
      // Valid from the start, with a random token, so the task starts before anyone edits it.
      generateSecretString: {
        secretStringTemplate: "{}",
        generateStringKey: "admin",
        excludePunctuation: true,
      },
    });

    const task = new FargateTaskDefinition(this, "Task", {
      cpu: 512,
      memoryLimitMiB: 1024,
      volumes: [
        {
          name: "data",
          efsVolumeConfiguration: {
            fileSystemId: fileSystem.fileSystemId,
            transitEncryption: "ENABLED",
            authorizationConfig: { accessPointId: accessPoint.accessPointId, iam: "ENABLED" },
          },
        },
      ],
    });
    fileSystem.grant(
      task.taskRole,
      "elasticfilesystem:ClientMount",
      "elasticfilesystem:ClientWrite",
    );
    for (const statement of bedrockStatements(this, [props.modelId, props.workerModelId])) {
      task.addToTaskRolePolicy(statement);
    }

    const container = task.addContainer("App", {
      image: ContainerImage.fromAsset(REPO_ROOT, { platform: Platform.LINUX_AMD64 }),
      portMappings: [{ containerPort: CONTAINER_PORT }],
      // The most Fargate allows, so a stopping task can finish and release its data lock.
      stopTimeout: Duration.seconds(120),
      logging: LogDrivers.awsLogs({
        streamPrefix: "ticketgate",
        logGroup: new LogGroup(this, "Logs", { retention: RetentionDays.ONE_MONTH }),
      }),
      environment: {
        LLM_MODE: "bedrock",
        BEDROCK_MODEL_ID: props.modelId,
        BEDROCK_WORKER_MODEL_ID: props.workerModelId,
        AWS_REGION: this.region,
        TICKET_SOURCE: "github",
        GITHUB_REPO: props.githubRepo,
        ...(props.githubLabelFilter ? { GITHUB_LABEL_FILTER: props.githubLabelFilter } : {}),
        TARGET_REPO_URL: props.targetRepoUrl,
        DATA_DIR: DATA_PATH,
        // SQLite's write-ahead log does not work on a network file system.
        SQLITE_JOURNAL_MODE: "DELETE",
        // A stopped task's lock can take a while to expire on a network file system.
        DATA_LOCK_WAIT_SECONDS: "120",
        POLL_INTERVAL_SECONDS: String(props.pollIntervalSeconds ?? 300),
      },
      secrets: {
        GITHUB_TOKEN: EcsSecret.fromSecretsManager(githubToken),
        REVIEWER_TOKENS: EcsSecret.fromSecretsManager(reviewerTokens),
      },
    });
    container.addMountPoints({
      sourceVolume: "data",
      containerPath: DATA_PATH,
      readOnly: false,
    });

    const service = new FargateService(this, "Service", {
      cluster: new Cluster(this, "Cluster", { vpc }),
      taskDefinition: task,
      assignPublicIp: false,
      // One SQLite file means one writer. A deployment stops the old task before starting the
      // new one, so it causes a short outage rather than two processes on the same data.
      desiredCount: 1,
      minHealthyPercent: 0,
      maxHealthyPercent: 100,
      circuitBreaker: { rollback: true },
      // Covers cloning the target repository, and waiting up to two minutes for the data lock.
      healthCheckGracePeriod: Duration.minutes(5),
    });
    fileSystem.connections.allowDefaultPortFrom(service);
    // Tasks that start before these exist fail to mount or clone, which counts towards rollback.
    service.node.addDependency(fileSystem.mountTargetsAvailable);
    for (const subnet of vpc.privateSubnets) {
      service.node.addDependency(subnet.internetConnectivityEstablished);
    }

    const albSecurityGroup = new SecurityGroup(this, "AlbSecurityGroup", {
      vpc,
      description: "Reviewer access to Ticketgate",
    });
    const port = props.certificateArn ? 443 : 80;
    if (props.allowedCidr) {
      albSecurityGroup.addIngressRule(Peer.ipv4(props.allowedCidr), Port.tcp(port));
    } else {
      Annotations.of(this).addWarning(
        "No allowedCidr was given, so nobody can reach the load balancer.",
      );
    }
    if (!props.certificateArn) {
      Annotations.of(this).addWarning(
        "No certificateArn was given, so reviewer tokens would cross the network unencrypted.",
      );
    }

    const alb = new ApplicationLoadBalancer(this, "Alb", {
      vpc,
      internetFacing: true,
      securityGroup: albSecurityGroup,
      idleTimeout: ALB_IDLE_TIMEOUT,
    });
    const listener = alb.addListener("Listener", {
      port,
      open: false,
      protocol: props.certificateArn ? ApplicationProtocol.HTTPS : ApplicationProtocol.HTTP,
      sslPolicy: props.certificateArn ? SslPolicy.RECOMMENDED_TLS : undefined,
      certificates: props.certificateArn
        ? [Certificate.fromCertificateArn(this, "Certificate", props.certificateArn)]
        : undefined,
    });
    listener.addTargets("App", {
      port: CONTAINER_PORT,
      protocol: ApplicationProtocol.HTTP,
      targets: [service],
      healthCheck: { path: "/api/health", interval: Duration.seconds(15), healthyThresholdCount: 2 },
      deregistrationDelay: Duration.seconds(30),
    });

    new CfnOutput(this, "Url", {
      value: props.certificateArn
        ? `https://${props.domainName}`
        : `http://${alb.loadBalancerDnsName}`,
    });
    new CfnOutput(this, "AlbDnsName", { value: alb.loadBalancerDnsName });
    new CfnOutput(this, "ClusterName", { value: service.cluster.clusterName });
    new CfnOutput(this, "ServiceName", { value: service.serviceName });
    new CfnOutput(this, "GithubTokenSecret", { value: githubToken.secretName });
    new CfnOutput(this, "ReviewerTokensSecret", { value: reviewerTokens.secretName });
  }
}
