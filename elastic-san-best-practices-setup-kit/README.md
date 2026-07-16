# Azure Elastic SAN with Windows VM and iSCSI MPIO (Best Practices Optimized)

This Terraform configuration deploys a complete Azure Elastic SAN solution with a Windows Server VM optimized for high-performance iSCSI connectivity using Multipath I/O (MPIO). The configuration incorporates **Azure Elastic SAN best practices** for maximum performance and reliability.

## Architecture Overview

- **Azure Elastic SAN**: 20 TiB Premium_LRS storage in East US Zone 1
- **Volume**: 1 TiB volume with iSCSI protocol
- **Windows VM**: Latest Windows Server 2022 Datacenter Azure Edition in Zone 1 with **Accelerated Networking**
- **Networking**: VNet with service endpoints (not private endpoints) for storage connectivity
- **MPIO**: Configured with 32 iSCSI sessions and **optimized registry settings**

## ✨ Best Practices Implemented

This configuration follows the official [Azure Elastic SAN best practices](https://learn.microsoft.com/azure/storage/elastic-san/elastic-san-best-practices#iscsi):

### 🚀 **Performance Optimizations**
✅ **Accelerated Networking**: Enabled for reduced latency and improved packet processing  
✅ **Gen 5 VM Series**: Uses D-series v3 for optimal storage performance  
✅ **Zone Alignment**: VM and Elastic SAN in same zone for minimal latency  
✅ **32 iSCSI Sessions**: Maximum supported sessions for single volume performance  

### ⚙️ **iSCSI Registry Optimizations** 
✅ **MaxTransferLength**: 256KB for optimal data transfer  
✅ **MaxBurstLength**: 256KB for improved SCSI payload handling  
✅ **FirstBurstLength**: 256KB for unsolicited data optimization  
✅ **MaxRecvDataSegmentLength**: 256KB for receive optimization  
✅ **InitialR2T**: Disabled for improved flow control  
✅ **ImmediateData**: Enabled for reduced latency  
✅ **Timeout Optimization**: 30-second timeouts for robust connectivity  

### 🔧 **MPIO Enhancements**
✅ **Round-Robin Load Balancing**: Optimal I/O distribution across paths  
✅ **30-Second Disk Timeout**: Prevents premature path failures  
✅ **Automatic iSCSI Claim**: Seamless multipath device detection  

## Features

✅ **Maximum Performance**: Implements all Azure-recommended optimizations  
✅ **Auto-Configuration**: Zero manual setup required post-deployment  
✅ **Production-Ready**: Enterprise-grade settings and timeouts  
✅ **Best Practices**: Follows Microsoft's official guidelines  
✅ **Comprehensive Logging**: Detailed setup and validation logs  

## Prerequisites

1. **Azure Subscription** with appropriate permissions
2. **Terraform** >= 1.0 installed
3. **Azure CLI** installed and authenticated (`az login`)
4. **Sufficient Quota** for:
   - Elastic SAN (20 TiB)
   - Standard_D4s_v3 VM in East US
   - Premium storage

## Quick Start

1. **Clone and Navigate**
   ```bash
   git clone https://github.com/Azure-Samples/azure-elastic-san.git
   cd azure-elastic-san/elastic-san-best-practices-setup-kit
   ```

2. **Configure Variables**
   ```bash
   cp terraform.tfvars.example terraform.tfvars
   # Edit terraform.tfvars with your values
   ```

3. **Initialize and Deploy**
   ```bash
   terraform init
   terraform plan
   terraform apply
   ```

4. **Connect to VM**
   ```bash
   # Use the RDP connection string from outputs
   terraform output rdp_connection_command
   ```

## Configuration Options

### VM Sizes (Optimized for Storage)
- `Standard_D48as_v7` (Default) - 48 vCPUs, 192 GB RAM, Premium SSD, High network bandwidth
- `Standard_D32as_v7` - 32 vCPUs, 128 GB RAM, Premium SSD
- `Standard_D16as_v7` - 16 vCPUs, 64 GB RAM, Premium SSD  
- `Standard_D8as_v7` - 8 vCPUs, 32 GB RAM, Premium SSD
- `Standard_D4s_v3` - 4 vCPUs, 16 GB RAM, Premium SSD (legacy)
- `Standard_E48as_v7` - 48 vCPUs, 384 GB RAM, Memory optimized

### iSCSI Session Count
- **32 sessions** (Default): Maximum performance for single volume
- **16 sessions**: Good balance for multiple volumes
- **8 sessions**: Basic MPIO for smaller workloads

## Deployment Details

### What Gets Created

1. **Resource Group**: Container for all resources
2. **Virtual Network**: 10.0.0.0/16 with storage service endpoint
3. **Subnet**: 10.0.2.0/24 for VM placement with service endpoints
4. **Network Security Group**: Rules for RDP (3389) and iSCSI (3260)
5. **Public IP**: Static IP for RDP access
6. **Network Interface**: VM connection with **Accelerated Networking enabled**
7. **Elastic SAN**: 20 TiB Premium storage in Zone 1
8. **Volume Group**: Container with service endpoint access
9. **Volume**: 1 TiB iSCSI volume
10. **Windows VM**: Zone 1 placement with managed identity
11. **Best Practice Configuration**: Automated iSCSI and MPIO optimization

### Automated Best Practice Configuration

The deployment automatically applies Azure's recommended optimizations:

**🔧 iSCSI Registry Settings**:
- MaxTransferLength: 256KB (262144 bytes)
- MaxBurstLength: 256KB for optimal SCSI payload
- FirstBurstLength: 256KB for unsolicited data
- MaxRecvDataSegmentLength: 256KB for receive optimization
- InitialR2T: Disabled for improved flow control
- ImmediateData: Enabled for reduced latency
- WMIRequestTimeout: 30 seconds for robust connectivity
- LinkDownTime: 30 seconds for optimal timeout handling

**🛣️ MPIO Configuration**:
- Round-robin load balancing across all paths
- 30-second disk timeout (prevents premature failures)
- Automatic multipath claim for iSCSI devices
- 32 high-performance iSCSI sessions per volume

**⚡ Network Optimizations**:
- Accelerated Networking enabled on VM NIC
- Service endpoints for direct Azure backbone connectivity
- Zone-aligned deployment for minimal latency

**💽 Storage Configuration**:
- Premium SSD storage for consistent performance
- Automatic disk initialization and formatting
- NTFS file system with optimal cluster size

## Post-Deployment

### 1. Automatic Setup Process

After deployment completes, the VM will automatically:
1. **Configure iSCSI and MPIO** with Azure best practices
2. **Schedule an automatic reboot** in 2 minutes to apply registry optimizations
3. **Run performance benchmarks** post-reboot to validate optimal configuration

**📊 Performance Benchmark Details**:
- **Quick Validation Test**: 4K mixed workload (60 seconds) for immediate feedback
- **I/O-Intensive Test**: 4K random I/O with 75% read/25% write pattern (15 minutes)
- **Throughput Test**: 1M sequential read workload (15 minutes)
- **Results**: Stored in `C:\ESANBenchmark.log` on the VM

💡 **Monitor Progress**: Check `C:\ESANSetup.log` for initial setup and `C:\ESANBenchmark.log` for benchmark results.

### 2. Connect via RDP
```powershell
# Get connection details
terraform output rdp_connection_command
terraform output admin_username
terraform output admin_password
```

### 2. Verify iSCSI Setup
```powershell
# Check iSCSI service
Get-Service -Name MSiSCSI

# List active sessions (should show 32)
iscsicli SessionList

# Check MPIO status and devices
mpclaim -s -d

# Verify registry optimizations
Get-ItemProperty -Path "HKLM:\SYSTEM\CurrentControlSet\Control\Class\{4d36e97b-e325-11ce-bfc1-08002be10318}\0004\Parameters" -Name MaxTransferLength

# View detailed setup log
Get-Content C:\ESANSetup.log

# Run comprehensive validation
.\validate-elastic-san.ps1
```

### 3. Performance Validation (Automatic)

**✅ Automated Process**: The VM automatically:
- Restarts 2 minutes after initial setup to apply registry optimizations
- Runs comprehensive DiskSpd performance benchmarks post-reboot
- Validates optimal iSCSI MPIO connectivity and performance

**📋 Benchmark Tests Performed**:
1. **Quick Validation**: 4K mixed I/O (60s) for immediate feedback
2. **I/O-Intensive**: 4K random with 75%/25% read/write (15min)
3. **Throughput**: 1M sequential read for backup scenarios (15min)

**📊 Results Location**: Check `C:\ESANBenchmark.log` for detailed performance metrics

### 4. Manual Performance Testing (Optional)
```powershell
# If you need additional custom testing, DiskSpd is already downloaded
# Location: C:\Users\%USERNAME%\AppData\Local\Temp\DiskSpd\

# Example test command
diskspd -c1G -d30 -r -w25 -t8 -o32 -b64k -Su -Rxml E:\testfile.dat
```

## Important Notes

### Performance Optimization
- **Zone Placement**: VM and Elastic SAN are in the same zone for minimal latency
- **32 Sessions**: Configured for maximum single-volume performance
- **Premium SSD**: Provides consistent high IOPS and low latency
- **Service Endpoints**: Direct Azure backbone connectivity

### Security Considerations
- **NSG Rules**: Only allows RDP from anywhere (consider restricting source IPs)
- **Service Endpoints**: More secure than public internet routing
- **Random Password**: Generated securely and stored in Terraform state

### Cost Considerations
- **Elastic SAN**: Premium storage is billed per TiB provisioned
- **VM**: Standard_D4s_v3 runs ~$150/month (East US pricing)
- **Networking**: Service endpoints have no additional cost

## Troubleshooting

### iSCSI Connection Issues
```powershell
# Check network connectivity
Test-NetConnection -ComputerName <portal-hostname> -Port 3260

# Restart iSCSI service
Restart-Service MSiSCSI

# Re-run setup script
C:\ESANSetup.log  # Check for errors
```

### MPIO Issues
```powershell
# Verify MPIO is installed
Get-WindowsFeature -Name Multipath-IO

# Check MPIO configuration
mpclaim -s -d

# Enable verbose MPIO logging
Set-MPIOSetting -NewPathRecoveryInterval 20 -NewPDORemovePeriod 120
```

### Common Issues
1. **Quota Exceeded**: Check Azure quotas for Elastic SAN and compute
2. **Zone Availability**: Ensure East US Zone 1 supports your VM size
3. **Network Issues**: Verify service endpoint is functioning
4. **Permission Issues**: Ensure managed identity has storage access

## Cleanup

```bash
# Destroy all resources
terraform destroy

# Verify cleanup in Azure portal
```

## Security Best Practices

1. **Restrict RDP Access**: Update NSG rules to allow only your IP range
2. **Use Bastion**: Consider Azure Bastion for secure VM access
3. **Enable Monitoring**: Add Azure Monitor for performance tracking
4. **Backup Strategy**: Implement backup for critical data
5. **Access Reviews**: Regular review of storage access permissions

## Related Documentation

- [Azure Elastic SAN Documentation](https://learn.microsoft.com/azure/storage/elastic-san/)
- [Windows iSCSI MPIO Setup](https://learn.microsoft.com/azure/storage/elastic-san/elastic-san-connect-windows)
- [Terraform AzureRM Provider](https://registry.terraform.io/providers/hashicorp/azurerm/latest)
- [Azure Storage Performance](https://learn.microsoft.com/azure/storage/common/storage-performance-checklist)

## Support

For issues with this Terraform configuration:
1. Check the troubleshooting section above
2. Review Terraform and Azure CLI outputs
3. Consult Azure Elastic SAN documentation
4. Open an issue with deployment logs and error details