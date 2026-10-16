// AI Workbench - Azure-native landing zone.
//
//   Container Apps (internal, VNet)  : api, worker (KEDA on Service Bus queue length), console, migrate job
//   PostgreSQL Flexible Server       : the only database (knowledge core, extract staging, jobs, checkpoints);
//                                      Entra auth only, pgvector allow-listed
//   Storage (Blob, block uploads)    : uploads (2-4 GB bundles, chunked), evidence, context
//   Service Bus (Premium, private)   : job queue - the job table in PostgreSQL stays the source of truth
//   Key Vault                        : admin password hash, token-signing key, PII HMAC key
//   Managed Identity everywhere      : no connection strings or keys in configuration
//   Azure OpenAI (optional)          : enable with deployAzureOpenAI once a model is approved for use

targetScope = 'resourceGroup'

@description('Short environment name, lower-case letters and digits, e.g. dev, test, prod')
@minLength(2)
@maxLength(8)
param env string
param location string = resourceGroup().location
@description('Object ID of the Entra group that administers PostgreSQL')
param pgAdminGroupObjectId string
param pgAdminGroupName string
@description('Image tag built by the pipeline')
param imageTag string
@description('Deploy an Azure OpenAI account with a private endpoint (LLM-assisted agents, embeddings)')
param deployAzureOpenAI bool = false
param workerMaxReplicas int = 10

var prefix = 'aiwb-${env}'
var tags = { application: 'ai-workbench', environment: env, dataClassification: 'confidential' }

// ------------------------------------------------------------------ network
resource vnet 'Microsoft.Network/virtualNetworks@2024-01-01' = {
  name: '${prefix}-vnet'
  location: location
  tags: tags
  properties: {
    addressSpace: { addressPrefixes: ['10.40.0.0/16'] }
    subnets: [
      {
        name: 'apps'
        properties: {
          addressPrefix: '10.40.0.0/23'
          delegations: [{ name: 'aca', properties: { serviceName: 'Microsoft.App/environments' } }]
        }
      }
      { name: 'private-endpoints', properties: { addressPrefix: '10.40.16.0/24', privateEndpointNetworkPolicies: 'Disabled' } }
      {
        name: 'postgres'
        properties: {
          addressPrefix: '10.40.17.0/24'
          delegations: [{ name: 'pg', properties: { serviceName: 'Microsoft.DBforPostgreSQL/flexibleServers' } }]
        }
      }
    ]
  }
}

// ------------------------------------------------------------------ identity + observability
resource uami 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: '${prefix}-workload'
  location: location
  tags: tags
}

resource logs 'Microsoft.OperationalInsights/workspaces@2023-09-01' = {
  name: '${prefix}-logs'
  location: location
  tags: tags
  properties: { sku: { name: 'PerGB2018' }, retentionInDays: 90 }
}

resource appi 'Microsoft.Insights/components@2020-02-02' = {
  name: '${prefix}-appi'
  location: location
  kind: 'web'
  tags: tags
  properties: { Application_Type: 'web', WorkspaceResourceId: logs.id, DisableLocalAuth: true }
}

// ------------------------------------------------------------------ secrets (values set out of band, never in templates)
resource kv 'Microsoft.KeyVault/vaults@2023-07-01' = {
  name: take(replace('${prefix}-kv', '-', ''), 24)
  location: location
  tags: tags
  properties: {
    tenantId: subscription().tenantId
    sku: { family: 'A', name: 'standard' }
    enableRbacAuthorization: true
    enablePurgeProtection: true
    softDeleteRetentionInDays: 90
    publicNetworkAccess: 'Disabled'
  }
}

// ------------------------------------------------------------------ object store (uploads, evidence, context)
resource storage 'Microsoft.Storage/storageAccounts@2023-05-01' = {
  name: 'aiwb${env}objects'          // 3-24 lower-case letters and digits
  location: location
  tags: tags
  kind: 'StorageV2'
  sku: { name: 'Standard_ZRS' }
  properties: {
    minimumTlsVersion: 'TLS1_2'
    allowBlobPublicAccess: false
    allowSharedKeyAccess: false
    publicNetworkAccess: 'Disabled'
    encryption: { services: { blob: { enabled: true } }, keySource: 'Microsoft.Storage' }
  }
}

resource blobService 'Microsoft.Storage/storageAccounts/blobServices@2023-05-01' = {
  parent: storage
  name: 'default'
  properties: {
    deleteRetentionPolicy: { enabled: true, days: 30 }
    containerDeleteRetentionPolicy: { enabled: true, days: 30 }
  }
}

resource containers 'Microsoft.Storage/storageAccounts/blobServices/containers@2023-05-01' = [for c in ['uploads', 'evidence', 'context']: {
  parent: blobService
  name: c
  properties: { publicAccess: 'None' }
}]

// raw upload bundles move to cool storage after 30 days (evidence stays hot for review)
resource lifecycle 'Microsoft.Storage/storageAccounts/managementPolicies@2023-05-01' = {
  parent: storage
  name: 'default'
  properties: {
    policy: {
      rules: [
        {
          name: 'uploads-to-cool'
          enabled: true
          type: 'Lifecycle'
          definition: {
            filters: { blobTypes: ['blockBlob'], prefixMatch: ['uploads/'] }
            actions: { baseBlob: { tierToCool: { daysAfterModificationGreaterThan: 30 } } }
          }
        }
      ]
    }
  }
}

// ------------------------------------------------------------------ PostgreSQL (the only database)
resource pg 'Microsoft.DBforPostgreSQL/flexibleServers@2024-08-01' = {
  name: '${prefix}-pg'
  location: location
  tags: tags
  sku: { name: env == 'prod' ? 'Standard_D8ds_v5' : 'Standard_D4ds_v5', tier: 'GeneralPurpose' }
  properties: {
    version: '16'
    storage: { storageSizeGB: 512, autoGrow: 'Enabled' }
    network: { delegatedSubnetResourceId: '${vnet.id}/subnets/postgres', publicNetworkAccess: 'Disabled' }
    authConfig: { activeDirectoryAuth: 'Enabled', passwordAuth: 'Disabled', tenantId: subscription().tenantId }
    highAvailability: { mode: env == 'prod' ? 'ZoneRedundant' : 'Disabled' }
    backup: { backupRetentionDays: 35, geoRedundantBackup: 'Enabled' }
  }
}

resource pgExtensions 'Microsoft.DBforPostgreSQL/flexibleServers/configurations@2024-08-01' = {
  parent: pg
  name: 'azure.extensions'
  properties: { value: 'VECTOR', source: 'user-override' }
}

resource pgAdmin 'Microsoft.DBforPostgreSQL/flexibleServers/administrators@2024-08-01' = {
  parent: pg
  name: pgAdminGroupObjectId
  properties: { principalType: 'Group', principalName: pgAdminGroupName, tenantId: subscription().tenantId }
}

// The workload identity signs in with Entra tokens (storage.entra_auth). It is registered as an Entra admin so
// the migrate job can apply DDL; split a separate migration identity if your DBA policy requires it.
module pgWorkload 'modules/pg-entra-admin.bicep' = {
  name: '${prefix}-pg-workload-admin'
  params: {
    serverName: pg.name
    principalId: uami.properties.principalId
    principalName: uami.name
  }
  dependsOn: [pgAdmin]
}

resource workbenchDb 'Microsoft.DBforPostgreSQL/flexibleServers/databases@2024-08-01' = {
  parent: pg
  name: 'aiwb'
}

// ------------------------------------------------------------------ job queue
resource sb 'Microsoft.ServiceBus/namespaces@2022-10-01-preview' = {
  name: '${prefix}-sb'
  location: location
  tags: tags
  sku: { name: 'Premium', tier: 'Premium', capacity: 1 }
  properties: { disableLocalAuth: true, publicNetworkAccess: 'Disabled', minimumTlsVersion: '1.2' }
}

resource jobsQueue 'Microsoft.ServiceBus/namespaces/queues@2022-10-01-preview' = {
  parent: sb
  name: 'aiwb-jobs'
  properties: { lockDuration: 'PT5M', maxDeliveryCount: 5, deadLetteringOnMessageExpiration: true }
}

// ------------------------------------------------------------------ registry
resource acr 'Microsoft.ContainerRegistry/registries@2023-07-01' = {
  name: 'aiwb${env}acr'
  location: location
  tags: tags
  sku: { name: 'Premium' }
  properties: { adminUserEnabled: false, publicNetworkAccess: 'Disabled' }
}

// ------------------------------------------------------------------ least-privilege role assignments
var roles = {
  blobDataContributor: 'ba92f5b4-2d11-453d-a403-e96b0029c9fe'
  kvSecretsUser: '4633458b-17de-408a-b874-0445c86b69e6'
  acrPull: '7f951dda-4ed3-4680-a7ca-43fe172d538d'
  monitoringPublisher: '3913510d-42f4-4e42-8a64-420c390055eb'
  sbSender: '69a216fc-b8fb-44d8-bc22-1f3c2552a14b'
  sbReceiver: '4f6d3b9b-027b-4f4c-9142-0e5a2a2247e0'
  openAIUser: '5e0bd9bd-7b93-4f28-af87-19fc36ad61bd'
}

resource blobRole 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(storage.id, uami.id, roles.blobDataContributor)
  scope: storage
  properties: {
    principalId: uami.properties.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roles.blobDataContributor)
  }
}

resource kvRole 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(kv.id, uami.id, roles.kvSecretsUser)
  scope: kv
  properties: {
    principalId: uami.properties.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roles.kvSecretsUser)
  }
}

resource acrRole 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(acr.id, uami.id, roles.acrPull)
  scope: acr
  properties: {
    principalId: uami.properties.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roles.acrPull)
  }
}

resource appiRole 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(appi.id, uami.id, roles.monitoringPublisher)
  scope: appi
  properties: {
    principalId: uami.properties.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roles.monitoringPublisher)
  }
}

resource sbSendRole 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(sb.id, uami.id, roles.sbSender)
  scope: sb
  properties: {
    principalId: uami.properties.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roles.sbSender)
  }
}

resource sbReceiveRole 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(sb.id, uami.id, roles.sbReceiver)
  scope: sb
  properties: {
    principalId: uami.properties.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roles.sbReceiver)
  }
}

// ------------------------------------------------------------------ private endpoints
var peTargets = [
  { name: 'kv', id: kv.id, group: 'vault' }
  { name: 'blob', id: storage.id, group: 'blob' }
  { name: 'acr', id: acr.id, group: 'registry' }
  { name: 'sb', id: sb.id, group: 'namespace' }
]

resource endpoints 'Microsoft.Network/privateEndpoints@2024-01-01' = [for t in peTargets: {
  name: '${prefix}-pe-${t.name}'
  location: location
  tags: tags
  properties: {
    subnet: { id: '${vnet.id}/subnets/private-endpoints' }
    privateLinkServiceConnections: [{ name: t.name, properties: { privateLinkServiceId: t.id, groupIds: [t.group] } }]
  }
}]

// ------------------------------------------------------------------ optional: Azure OpenAI
resource aoai 'Microsoft.CognitiveServices/accounts@2024-10-01' = if (deployAzureOpenAI) {
  name: '${prefix}-aoai'
  location: location
  tags: tags
  kind: 'OpenAI'
  sku: { name: 'S0' }
  properties: { customSubDomainName: '${prefix}-aoai', publicNetworkAccess: 'Disabled', disableLocalAuth: true }
}

resource aoaiRole 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (deployAzureOpenAI) {
  name: guid(resourceGroup().id, uami.id, roles.openAIUser)
  scope: aoai
  properties: {
    principalId: uami.properties.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roles.openAIUser)
  }
}

// ------------------------------------------------------------------ Container Apps
resource cae 'Microsoft.App/managedEnvironments@2024-03-01' = {
  name: '${prefix}-cae'
  location: location
  tags: tags
  properties: {
    vnetConfiguration: { infrastructureSubnetId: '${vnet.id}/subnets/apps', internal: true }
    workloadProfiles: [
      { name: 'Consumption', workloadProfileType: 'Consumption' }
      { name: 'ingest', workloadProfileType: 'D8', minimumCount: 0, maximumCount: workerMaxReplicas }
    ]
    appLogsConfiguration: { destination: 'log-analytics', logAnalyticsConfiguration: {
      customerId: logs.properties.customerId, sharedKey: logs.listKeys().primarySharedKey } }
  }
}

var image = '${acr.properties.loginServer}/ai-workbench-api:${imageTag}'
var secrets = [for s in ['admin-password-hash', 'auth-signing-key', 'pii-hmac-key']: {
  name: s
  keyVaultUrl: 'https://${kv.name}${environment().suffixes.keyvaultDns}/secrets/${s}'
  identity: uami.id
}]
var commonEnv = [
  { name: 'AZURE_CLIENT_ID', value: uami.properties.clientId }
  { name: 'AIWB__STORAGE__DATABASE_URL', value: 'postgresql+psycopg://${uami.name}@${pg.properties.fullyQualifiedDomainName}:5432/aiwb?sslmode=require' }
  { name: 'AIWB__STORAGE__BLOB_ACCOUNT_URL', value: storage.properties.primaryEndpoints.blob }
  { name: 'AIWB__JOBS__SERVICE_BUS_NAMESPACE', value: '${sb.name}.servicebus.windows.net' }
  { name: 'AIWB__JOBS__SERVICE_BUS_QUEUE', value: jobsQueue.name }
  { name: 'APPLICATIONINSIGHTS_CONNECTION_STRING', value: appi.properties.ConnectionString }
  { name: 'AIWB_ADMIN_PASSWORD_HASH', secretRef: 'admin-password-hash' }
  { name: 'AIWB_AUTH_SIGNING_KEY', secretRef: 'auth-signing-key' }
  { name: 'AIWB_PII_HMAC_KEY', secretRef: 'pii-hmac-key' }
]
var registries = [{ server: acr.properties.loginServer, identity: uami.id }]
var identity = { type: 'UserAssigned', userAssignedIdentities: { '${uami.id}': {} } }

resource api 'Microsoft.App/containerApps@2024-03-01' = {
  name: '${prefix}-api'
  location: location
  tags: tags
  identity: identity
  properties: {
    environmentId: cae.id
    workloadProfileName: 'Consumption'
    configuration: {
      ingress: { external: false, targetPort: 8000, transport: 'http' }
      secrets: secrets
      registries: registries
    }
    template: {
      containers: [{
        name: 'api'
        image: image
        resources: { cpu: json('2'), memory: '4Gi' }
        env: commonEnv
        probes: [
          { type: 'Readiness', httpGet: { path: '/v1/health', port: 8000 }, periodSeconds: 10 }
          { type: 'Liveness', httpGet: { path: '/v1/health', port: 8000 }, periodSeconds: 30 }
        ]
      }]
      scale: {
        minReplicas: env == 'prod' ? 2 : 1
        maxReplicas: 6
        rules: [{ name: 'http', http: { metadata: { concurrentRequests: '50' } } }]
      }
    }
  }
}

// workers stream 2-4 GB bundles (decompress, decode, COPY into staging): a dedicated profile with local disk
// scale rule authenticates to Service Bus with the managed identity (API 2024-10-02-preview)
resource worker 'Microsoft.App/containerApps@2024-10-02-preview' = {
  name: '${prefix}-worker'
  location: location
  tags: tags
  identity: identity
  properties: {
    environmentId: cae.id
    workloadProfileName: 'ingest'
    configuration: { secrets: secrets, registries: registries }
    template: {
      containers: [{
        name: 'worker'
        image: image
        command: ['python', 'cli.py', 'worker']
        resources: { cpu: json('4'), memory: '16Gi' }
        env: commonEnv
      }]
      scale: {
        minReplicas: 0
        maxReplicas: workerMaxReplicas
        rules: [{
          name: 'jobs'
          custom: {
            type: 'azure-servicebus'
            metadata: { namespace: sb.name, queueName: jobsQueue.name, messageCount: '1' }
            identity: uami.id
          }
        }]
      }
    }
  }
}

// schema migrations run once per release, before the new api / worker revisions take traffic
resource migrate 'Microsoft.App/jobs@2024-03-01' = {
  name: '${prefix}-migrate'
  location: location
  tags: tags
  identity: identity
  properties: {
    environmentId: cae.id
    workloadProfileName: 'Consumption'
    configuration: {
      triggerType: 'Manual'
      replicaTimeout: 1800
      replicaRetryLimit: 1
      manualTriggerConfig: { parallelism: 1, replicaCompletionCount: 1 }
      secrets: secrets
      registries: registries
    }
    template: {
      containers: [{
        name: 'migrate'
        image: image
        command: ['python', 'cli.py', 'migrate']
        resources: { cpu: json('1'), memory: '2Gi' }
        env: commonEnv
      }]
    }
  }
}

resource console 'Microsoft.App/containerApps@2024-03-01' = {
  name: '${prefix}-console'
  location: location
  tags: tags
  identity: identity
  properties: {
    environmentId: cae.id
    workloadProfileName: 'Consumption'
    configuration: {
      ingress: { external: true, targetPort: 8080, transport: 'http' }     // external to the internal environment = VNet only
      registries: registries
    }
    template: {
      containers: [{
        name: 'console'
        image: '${acr.properties.loginServer}/ai-workbench-console:${imageTag}'
        resources: { cpu: json('0.5'), memory: '1Gi' }
        env: [{ name: 'API_UPSTREAM', value: 'http://${api.name}' }]
      }]
      scale: { minReplicas: 1, maxReplicas: 3 }
    }
  }
}

output workloadIdentityClientId string = uami.properties.clientId
output blobAccountUrl string = storage.properties.primaryEndpoints.blob
output postgresHost string = pg.properties.fullyQualifiedDomainName
output serviceBusNamespace string = '${sb.name}.servicebus.windows.net'
output keyVaultUri string = kv.properties.vaultUri
output consoleFqdn string = console.properties.configuration.ingress.fqdn
output migrateJobName string = migrate.name
