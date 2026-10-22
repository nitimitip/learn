/******************************************************************************
  BUG FIX BF01 - PENDING VALIDATION - DO NOT DEPLOY UNTESTED
  Procedure : [dbo].[usp_QMS_FinalDataLoad_CRM_MajorWork]
  Base      : QPID_SP/usp_QMS_FinalDataLoad_CRM_MajorWork.sql (production original)
  Fix       : BF01 (2026-10-21)
  Generated : bugfix/tools/gen_bugfix.py (inverse transform = base, checked)
  Deploy    : together with the other CRM loader (usp_QMS_FinalDataLoad_CRM_SmallWork);
              both must use the same run lock.

  WHY (BF01, user report 2026-10-21: the CRM load stops until someone
  clears a message by hand):
    The loader sets BT_Message.Message_Status = 'I' OUTSIDE a transaction
    and opens BEGIN TRAN ~400 lines later; only the CATCH 'F' update ever
    moves the row out of 'I'. Both loaders start with
    IF EXISTS (... MESSAGE_STATUS='I') RETURN with no [Source] filter, so
    ONE stuck row stops BOTH loaders. The row stays 'I' when:
    a. the run is killed or times out (attention): CATCH never runs;
    b. a compile-time error (deferred name resolution: missing object or
       column) in the procedure's own scope: TRY/CATCH does not catch it;
    c. the 'F' update itself fails: @pErrorMessage is up to 5000 chars,
       Error_Description is varchar(500) (e.g. a conversion error that
       quotes a long value);
    d. CATCH starts with a bare ROLLBACK TRAN, which raises 3903 when the
       error came before BEGIN TRAN. Whether the rest of CATCH still runs
       after it depends on the caller (outer TRY or not); bf01_validation
       BF01.4 shows the behaviour on this server. Not assumed either way.

  WHAT CHANGED (and nothing else):
    a. The 'I'-row check is replaced by sp_getapplock on 'QPID_CRM_FinalDataLoad'
       (Exclusive, Session owner, timeout 0), shared by both loaders, so
       they still never run at the same time. If the lock is held: print
       'Request in progress' and RETURN, as before. The lock is released at
       the end of TRY, in CATCH, and automatically when the session ends.
    b. With the lock held no loader is running, so any 'I' row is left by a
       dead run: it is set to 'F' with a BF01 Error_Description (visible,
       not reprocessed automatically; set it to 'W' to retry).
    c. CATCH: IF @@TRANCOUNT > 0 ROLLBACK TRAN.
    d. CATCH 'F' update: LEFT(@pErrorMessage, 500) and AND Message_Status =
       'I', so an error after COMMIT (SP_LCT_MapEquipmentDataToTechType)
       no longer tries to turn a committed 'P' into 'F'.
    e. CREATE -> ALTER (production-original variant only).

  BEHAVIOUR: a message that loads without error is processed exactly as
  before (same statements, same transaction, same isolation level). The
  difference is only on the failure path and in the run lock. NOT added:
  SET XACT_ABORT ON (it would doom the transaction on errors that nested
  procedures swallow today and turn those loads into failures).

  UNCHANGED (inherited, NOT fixed): MajorWork sets 'I' / 'P' / 'F' by
  QMS_Reference_Number, so every BT_Message row with that reference (any
  source, any age) is updated together; READ UNCOMMITTED transaction;
  MAX()+1 key generation (Batch 34 issue 2).

  VALIDATION: bugfix/validation/bf01_validation.sql
  ROLLBACK  : bugfix/rollback/usp_QMS_FinalDataLoad_CRM_MajorWork_ORIGINAL.sql
******************************************************************************/
SET ANSI_NULLS ON
GO
SET QUOTED_IDENTIFIER ON
GO
ALTER PROCEDURE [dbo].[usp_QMS_FinalDataLoad_CRM_MajorWork]
	
	@pErrorMessage	VARCHAR(5000)=NULL OUTPUT 
AS
BEGIN
	SET NOCOUNT ON;
	DECLARE @vcntBtMsg INT; -- Record count of CTE
	DECLARE @vpageSize INT; -- For pagination
	DECLARE @vpageNumber INT; -- For pagination
	DECLARE @i INT; -- For iteration
	DECLARE @pQMSRefNo VARCHAR(50); 
	DECLARE @ENQAEID INT;
	DECLARE @vAE_internalRef VARCHAR(50);
	DECLARE @vOwnerOnly_OrgName VARCHAR(50)='';
	DECLARE @vOwnerOnly_Telephone VARCHAR(50)='';
	DECLARE @vOwnerOnly_Custtype VARCHAR(50)='';
	DECLARE @cnt INT;
	DECLARE @vOwnerOrAgentOccupier VARCHAR(50)=NULL;
	DECLARE @vdomesticorCommercial VARCHAR(20);
	DECLARE @vdomesticorCommercial_A VARCHAR(20);
	
	DECLARE @vCUST int;
	DECLARE @vcntAgnt INT
	DECLARE @ccustid INT;
	DECLARE @ploggedonUser INT;
	DECLARE @ccustPremAddrID INT;
	DECLARE @addressLine1 VARCHAR(50);
	DECLARE @addressLine2 VARCHAR(50);
	DECLARE @addressLine3 VARCHAR(50);
	DECLARE @addressLine4 VARCHAR(50);
	DECLARE @addressLine5 VARCHAR(50);
	DECLARE @addressLine6 VARCHAR(50);
	DECLARE @addressLine7 VARCHAR(50);
	DECLARE @addressLine8 VARCHAR(50);
	DECLARE @addressLine9 VARCHAR(50);

	DECLARE @addressLine1_stg VARCHAR(50);
	DECLARE @addressLine2_stg VARCHAR(50);
	DECLARE @addressLine3_stg VARCHAR(50);
	DECLARE @addressLine4_stg VARCHAR(50);
	DECLARE @addressLine5_stg VARCHAR(50);
	DECLARE @addressLine6_stg VARCHAR(50);
	DECLARE @addressLine7_stg VARCHAR(50);
	DECLARE @addressLine8_stg VARCHAR(50);
	DECLARE @addressLine9_stg VARCHAR(50);
	DECLARE @addressPostcode_stg VARCHAR(50);
	DECLARE @addressEasting_stg VARCHAR(10);
	DECLARE @addressNorthing_stg VARCHAR(10);
	DECLARE @addressDesc_stg VARCHAR(255);
	DECLARE @addressSitename_stg VARCHAR(250);
	DECLARE @addressCFSID_stg INT;
	DECLARE @billingPostCode_stg VARCHAR(50);

	DECLARE @ccustPremAddrIDBKP INT;
	DECLARE @ccustPremConIDBKP INT;
	DECLARE @vAE_assignedToDept VARCHAR(50);
	DECLARE @vDepartment_Qpid_value INT;
	DECLARE @vAEAddress INT=0;
	DECLARE @vAddressId BIGINT=0;
	DECLARE @vCust_ID BIGINT=0;
	DECLARE @con_forename VARCHAR(50);
	DECLARE @con_surname VARCHAR(50);
	DECLARE @con_personalTitle VARCHAR(50);
	DECLARE @con_telephone VARCHAR(50);
	DECLARE @con_mobile VARCHAR(50);
	DECLARE @con_email VARCHAR(50);

	DECLARE @con_forename_stg_a VARCHAR(50);
	DECLARE @con_surname_stg_a VARCHAR(50);
	DECLARE @con_personalTitle_stg_a VARCHAR(50);
	DECLARE @con_telephone_stg_a VARCHAR(50);
	DECLARE @con_forename_stg VARCHAR(50);
	DECLARE @con_surname_stg VARCHAR(50);
	DECLARE @con_personalTitle_stg VARCHAR(50);
	DECLARE @con_telephone_stg VARCHAR(50);
	DECLARE @con_mobile_stg VARCHAR(50);
	DECLARE @con_email_stg VARCHAR(50);	
	DECLARE @ccustPremConID INT;
	DECLARE @QuoteDesigner VARCHAR(50);
	DECLARE @QuoteDesignerId INT;
	DECLARE @vCustomerAddressId int;
	DECLARE @VEnqId int;
	DECLARE @vQuoteId int;
	DECLARE @newAEID int;
	DECLARE @networkdist int;
	DECLARE @ae_address int;
	DECLARE @vAddIdA int;
	DECLARE @vCustId int;
	DECLARE @vAddIdS int;
	DECLARE @vConId int;
	DECLARE @vAE_ExternalRef3 VARCHAR(50);
	DECLARE @vOwnerOccupiedAddrId int;
	DECLARE @EX_actualSecondComer VARCHAR(50)=NULL;
	DECLARE @pCategory_addHist VARCHAR(50)=NULL;
	DECLARE @ptype_addHist INT=0;
	DECLARE @pStatus_addHist VARCHAR(50)=NULL;
	DECLARE @iAE_contractZone varchar(100);
	DECLARE @vAddIdO int;
	DECLARE @ExecStartTime datetime;
	DECLARE @ExecEndTime datetime;
	DECLARE @contactID int;
	DECLARE @MsgTxt varchar(2000);
	DECLARE @assignToPersonID INT;
	DECLARE @ae_assignedToPerson VARCHAR(50);
	DECLARE @defaultPersonID INT;
	DECLARE @defaultDeptID INT;
	DECLARE @correspondenceAddrId BIGINT;
	DECLARE @quoteStatus VARCHAR(50);
	DECLARE @quoteStatusID VARCHAR(50);
	DECLARE @agentCompany VARCHAR(250);
	DECLARE @company VARCHAR(250);
	DECLARE @agentName VARCHAR(250);
	DECLARE @vMaxStdId int;
	DECLARE @connectionnature VARCHAR(250);
	DECLARE @connectiontypechange VARCHAR(250);
	
	BEGIN TRY
	
		--Return if any request is in progress
		--BF01: the run lock is a session app lock shared by both CRM loaders. It is
		--released when the session ends, so a failed or killed run cannot block the loaders.
		DECLARE @vRunLock INT;
		EXEC @vRunLock = sp_getapplock @Resource = 'QPID_CRM_FinalDataLoad', @LockMode = 'Exclusive', @LockOwner = 'Session', @LockTimeout = 0;
		IF @vRunLock < 0
		BEGIN
			print 'Request in progress'
			RETURN;
		END
		--BF01: no loader is running, so an 'I' row is left over from a run that died
		UPDATE BT_Message SET Message_Status = 'F', Error_Description = 'BF01: in progress status left by an interrupted run; set to W to reprocess' WHERE Message_Status = 'I';
		print 'Begin process'
		SET @pErrorMessage = '0';
			
		
		SELECT @ploggedonUser= INT_USR_ID FROM MT_USERS WHERE [USER_NAME]='AD03\CRM.User'
		
	--Assignment of default value
	
		SET @vpageSize = 1;
		SET @vpageNumber = 0;
		SET @i = 0;

		
		--Get Bt_Message data in temp table
		SELECT TOP 1 * INTO #BTMsgTmp FROM BT_MESSAGE WITH (NOLOCK) WHERE Message_Status = 'W' AND [Source] = 'CRM_MajorWork';

		-- Set record count of temp table
		SELECT @vcntBtMsg=COUNT(1) FROM #BTMsgTmp;

		WHILE @i < @vcntBtMsg
		BEGIN
		
			--Set Start time
			SET @ExecStartTime = GETDATE();
			SET @MsgTxt = 'START '+CONVERT( VARCHAR(24), @ExecStartTime, 121)+', '
			
			--Reseting values
			
			SET @assignToPersonID = null
			SET @vDepartment_Qpid_value = null
			SET @billingPostCode_stg = null
			
			
			--Fetch record from Temp table one by one
			SELECT 
				@pQMSRefNo = QMS_REFERENCE_NUMBER,
				@quoteStatus = QUOTE_STATUS
			FROM 
				#BTMsgTmp 
			ORDER BY 
				QMS_REFERENCE_NUMBER 
			OFFSET 
				@vpageNumber 
			ROWS FETCH NEXT 
				@vpageSize ROWS ONLY
				
			print @pQMSRefNo

			--Set in progress status in BT_Message
			UPDATE
				BT_Message
			SET
				Message_Status = 'I'
			WHERE
				QMS_REFERENCE_NUMBER = @pQMSRefNo;

			SET @MsgTxt = @MsgTxt + CONVERT(VARCHAR, @pQMSRefNo)+', '
			
			--Find Quote status ID from LOV table
			/*SELECT
				@quoteStatusID=LOV_VALUE
			FROM
				MT_SYS_LOV 
			WHERE 
				lov_name='sysStatusQuote' 
				and LOV_DELETED=0 
				and LOV_OBSOLETE='N' 
				and LOV_DESC=@quoteStatus;*/
				
			--Select Enquiry Id and Enquiry Ref. from BT_ENQUIRY_MASTER based on Qpid ref. no of selected BT_MESSAGE row ##changed
			SELECT 
				@ENQAEID= AE_AEID , 
				@vAE_internalRef = AE_internalRef, 
				@vCUST = AE_customer, 
				@ae_address=AE_CUSTOMERADDRESS,--AE_address,
				@contactID = AE_Contact
			FROM 
				BT_ENQUIRY_MASTER 
			WITH (NOLOCK)
			WHERE 
				AE_internalRef=(SELECT 
									QPID_Reference_Number
								FROM 
									#BTMsgTmp 
								WHERE 
									QMS_Reference_Number=@pQMSRefNo)  
			SET @MsgTxt = @MsgTxt + 'AE enq,addr,cont ' + CONVERT(VARCHAR,@ENQAEID)+','+ CONVERT(VARCHAR,@ae_address)+','+ CONVERT(VARCHAR,@contactID)+', '
			--##Added
			SET @VEnqId = @ENQAEID
			SET @vCustId = @vCUST
			SET @correspondenceAddrId = @ae_address
			
			--Find the enquiry is for agent or owner
			SELECT  
				@vAE_ExternalRef3 = AE_ExternalRef3 
			FROM 
				BT_ENQUIRY_MASTER_CRM_STG 
			WITH (NOLOCK)
			WHERE 
				QMS_REF = @pQMSRefNo
			SET @MsgTxt = @MsgTxt + @vAE_ExternalRef3 + ', '

			--Select owner org. name, telephone number and customer type to search customer
			SELECT 
				TOP 1 @vOwnerOnly_OrgName = ISNULL(LTRIM(RTRIM(((CUST_ORG_NAME)))),''), 
				@vOwnerOnly_Telephone = ISNULL(LTRIM(RTRIM(((CUST_TELEPHONE)))),''), 
				@vOwnerOnly_Custtype = ISNULL(LTRIM(RTRIM(((CUST_TYPE)))),'')  
			FROM 
				BT_CUSTOMER_MASTER_CRM_STG 
			WITH (NOLOCK)
			WHERE 
				QMS_REF=@pQMSRefNo 
			ORDER BY ID DESC
						
			
			-- Select cust id for selected bt message record **
			-- To Do select cust id from enq master only
			/*SELECT 
				@vCUST = CUST_ID
			FROM 
				[BT_CUSTOMER_MASTER] cm
			INNER JOIN 
				BT_ENQUIRY_MASTER enq
			ON 
				enq.AE_customer = cm.cust_id	
			INNER JOIN 
				BT_CUSTOMER_MASTER_CRM_stg custg
			ON 
				custg.QMS_REF = enq.AE_externalRef2
			WHERE 
				enq.AE_externalRef2 = @pQMSRefNo*/

			--Select the BT message record is domestic or commercial
			SELECT 
				TOP 1 @vdomesticorCommercial = domesticorCommercial, 
				@vdomesticorCommercial_A = domesticorCommercial_A,
				@agentCompany = ISNULL(LTRIM(RTRIM(((Agent_Company)))),''),
				@company = ISNULL(LTRIM(RTRIM(((Company)))),''),
				@agentName = ISNULL(LTRIM(RTRIM(((con_personalTitle_A)))),'')+' '+ISNULL(LTRIM(RTRIM(((con_forename_A)))),'')+' '+ISNULL(LTRIM(RTRIM(((con_surname_A)))),'')
			FROM 
				BT_CONTACT_MASTER_CRM_STG 
			WITH (NOLOCK)
			WHERE 
				QMS_REF = @pQMSRefNo 
			ORDER BY ID DESC
			SET @MsgTxt = @MsgTxt + 'AgntCompny:'+@agentCompany+' Company:'+@company+' , '
			--Set customer Name based on Agent or Owner
			IF @vAE_ExternalRef3 = 'Agent'
			BEGIN
				IF LEN(@agentCompany)>0
				BEGIN
					set @vOwnerOnly_OrgName = @agentCompany;
				END
				ELSE
				BEGIN
					set @vOwnerOnly_OrgName = @agentName;
				END
			END
			IF @vAE_ExternalRef3 = 'Owner'
			BEGIN
				IF LEN(@company)>0
				BEGIN
					set @vOwnerOnly_OrgName = @company;
				END
			END
			
			--Select record count of customer master if any record match with the selected owner org name, telephone and cust type
			SELECT 
				@cnt = COUNT(1) 
			FROM 
				BT_CUSTOMER_MASTER CCM 
			WITH (NOLOCK)
			WHERE 
				CCM.CUST_ORG_NAME=@vOwnerOnly_OrgName
				AND CCM.CUST_TELEPHONE=@vOwnerOnly_Telephone 
				AND CCM.CUST_TYPE=@vOwnerOnly_Custtype
				
			--Select CUST_PRIM_ADDRID from Enquiry master for selected enquiry (used in enq update)  ##Changed
			--To Do select addr id  based on cust id
			SELECT  
				@vCustomerAddressId = cm.CUST_PRIM_ADDRID
			FROM
				BT_CUSTOMER_MASTER cm
			WITH (NOLOCK)
			WHERE
				CUST_ID = @vCUST
			/*FROM 
				BT_ENQUIRY_MASTER enq
			INNER JOIN 
				BT_CUSTOMER_MASTER cm
			ON 
				enq.AE_customer = cm.CUST_ID
			WHERE
				enq.AE_internalRef = @vAE_internalRef*/

			-- select AE_assignedToDept to select  Department_Qpid_value from DepartmentType_QPID_QMS_Map **
			SELECT  
				@vAE_assignedToDept = AE_assignedToDept,
				@ae_assignedToPerson = AE_assignedToPerson				
			FROM 
				BT_ENQUIRY_MASTER_CRM_STG 
			WITH (NOLOCK)
			WHERE 
				QMS_REF = @pQMSRefNo
			
			--select  Department_Qpid_value for enq update
			/*SELECT 
				TOP 1  @vDepartment_Qpid_value = Department_Qpid_value
			FROM 
				DepartmentType_QPID_QMS_Map
			WITH (NOLOCK)
			WHERE 
				Region_QMS = @vAE_assignedToDept*/
			SELECT 
					@vDepartment_Qpid_value = PEOPORG_PEOPORGID 
			FROM 
				MT_ACT_PEOPORG 
			WITH (NOLOCK)
			WHERE 
				PEOPORG_ASSIGNEEDEPT= 'Y' 
				AND LTRIM(RTRIM(UPPER(PEOPORG_NAME)))=@vAE_assignedToDept
				AND PEOPORG_LISTINENQUIRY = 'Y' 
				AND PEOPORG_DELETED <> 'Y'
				
			SELECT 
				@assignToPersonID = PEOPORG_PEOPORGID 
			FROM 
				MT_ACT_PEOPORG 
			WITH (NOLOCK)
			WHERE 
				PEOPORG_ASSIGNEEINDIV= 'Y' 
				AND LTRIM(RTRIM(UPPER(PEOPORG_NAME)))=RTRIM(LTRIM(UPPER(@ae_assignedToPerson))) 
				AND PEOPORG_LISTINENQUIRY = 'Y' 
				AND PEOPORG_DELETED <> 'Y'
				
			--Default value for person
			SELECT 
				@defaultPersonID = PEOPORG_PEOPORGID 
			FROM 
				MT_ACT_PEOPORG
			WITH (NOLOCK) 
			WHERE 
				PEOPORG_ASSIGNEEINDIV= 'Y' 
				AND LTRIM(RTRIM(UPPER(PEOPORG_NAME)))='NOT ALLOCATED'
				AND PEOPORG_LISTINENQUIRY = 'Y' 
				AND PEOPORG_DELETED <> 'Y'
					
			SET @assignToPersonID = CASE WHEN @assignToPersonID IS NULL THEN @defaultPersonID ELSE @assignToPersonID END	
				
			--For default value for dept
			SELECT  
				@defaultDeptID = PEOPORG_PEOPORGID
			FROM 
				MT_ACT_PEOPORG
			WITH (NOLOCK)
			WHERE 
				PEOPORG_ASSIGNEEDEPT = 'Y'
				AND PEOPORG_LISTINENQUIRY = 'Y'
				AND PEOPORG_DELETED <> 'Y' 
				AND LTRIM(RTRIM(UPPER(PEOPORG_NAME)))='NOT ALLOCATED'
					
			SET @vDepartment_Qpid_value = CASE WHEN @vDepartment_Qpid_value IS NULL THEN @defaultDeptID ELSE @vDepartment_Qpid_value END
			
			--Select address id, enquiry id, cust id from enquiry
			-- To Do Eleminate
			/*SELECT @VEnqId =AE_AEID,@ae_address=AE_address,@vCustId=AE_customer 
			FROM BT_ENQUIRY_MASTER WHERE AE_externalRef2 = @pQMSRefNo;*/
		
			

			--Select QuoteDesigner to find respective ID, which is required to insert quote
			SELECT  
				@QuoteDesigner = QUOSTG.QuoreDesigner
			FROM 
				[BT_QUOTE_MASTER_CRM_STG] QUOSTG
			WITH (NOLOCK)	
			WHERE 
				QUOSTG.QMS_REF = @pQMSRefNo
				
			--print @QuoteDesigner
		
			--For Default value
			SET @QuoteDesigner = ISNULL(UPPER(rtrim(ltrim(@QuoteDesigner))),'NOT ALLOCATED');
			--print @QuoteDesigner
			--Select QuoteDesignerId, which is required to insert quote 
			SELECT 
				TOP 1 @QuoteDesignerId =  PEOPORG_PEOPORGID 
			FROM 
				MT_ACT_PEOPORG 
			WITH (NOLOCK)
			WHERE 
				PEOPORG_ASSIGNEEINDIV= 'Y' 
				AND LTRIM(RTRIM(UPPER(PEOPORG_NAME)))=RTRIM(LTRIM(UPPER(@QuoteDesigner))) 
				AND peopOrg_designer = 'Y' AND PEOPORG_DELETED <> 'Y'
			
			

			--Select all address lines from stg table for selected bt message record
			
			IF @vAE_ExternalRef3 = 'Agent'
			BEGIN
				SET @MsgTxt = @MsgTxt + 'Inside Agent, '
				SELECT 
					@addressLine1_stg=ISNULL(UPPER(rtrim(ltrim(addressLine1_a))),''),					
					@addressLine2_stg=ISNULL(UPPER(rtrim(ltrim(addressLine2_a))),''),
					@addressLine3_stg=ISNULL(UPPER(rtrim(ltrim(addressLine3_a))),''),
					@addressLine4_stg=ISNULL(UPPER(rtrim(ltrim(addressLine4_a))),''),
					@addressLine5_stg=ISNULL(UPPER(rtrim(ltrim(addressLine5_a))),''),
					@addressLine6_stg=ISNULL(UPPER(rtrim(ltrim(addressLine6_a))),''),
					@addressLine7_stg=ISNULL(UPPER(rtrim(ltrim(addressLine7_a))),''),
					@addressLine8_stg=ISNULL(UPPER(rtrim(ltrim(addressLine8_a))),''),
					@addressLine9_stg=ISNULL(UPPER(rtrim(ltrim(addressLine9_a))),''),
					@addressPostcode_stg=ISNULL(UPPER(rtrim(ltrim(postCode_a))),''),
					@addressEasting_stg =ISNULL(UPPER(rtrim(ltrim(easting_a))),''),
					@addressNorthing_stg=ISNULL(UPPER(rtrim(ltrim(northing_a))),''),
					@addressDesc_stg=ISNULL(UPPER(rtrim(ltrim(description_a))),''),
					@addressSiteName_stg=ISNULL(UPPER(rtrim(ltrim(siteName_a))),''),
					@addressCFSID_stg=CFSID_a
				FROM 
					BT_ADDRESS_MASTER_CRM_STG 
				WITH (NOLOCK)
				WHERE 
					QMS_REF=@pQMSRefNo
				print 'Addr 1'+@addressLine1_stg
				SET @MsgTxt = @MsgTxt + 'addr_stg '+ isnull(@addressLine1_stg,'') + ', '
				--Select contacts from stg table for selected bt message record
				SELECT 
					@con_forename_stg=ISNULL(UPPER(rtrim(ltrim(con_forename_a))),''),
					@con_surname_stg=ISNULL(UPPER(rtrim(ltrim(con_surname_a))),''),
					@con_personalTitle_stg=ISNULL(UPPER(rtrim(ltrim(con_personalTitle_a))),''),
					@con_telephone_stg=ISNULL(UPPER(rtrim(ltrim(con_telephone_a))),''),
					@con_mobile_stg=ISNULL(UPPER(rtrim(ltrim(con_mobile_a))),''),
					@con_email_stg=ISNULL(UPPER(rtrim(ltrim(con_email_a))),''),
					@con_forename_stg_a=ISNULL(UPPER(rtrim(ltrim(con_forename_a))),''),
					@con_surname_stg_a=ISNULL(UPPER(rtrim(ltrim(con_surname_a))),''),
					@con_personalTitle_stg_a=ISNULL(UPPER(rtrim(ltrim(con_personalTitle_a))),''),
					@con_telephone_stg_a=ISNULL(UPPER(rtrim(ltrim(con_telephone_a))),'')
				FROM 
					BT_CONTACT_MASTER_CRM_STG 
				WITH (NOLOCK)
				WHERE 
					QMS_REF=@pQMSRefNo
				SET @MsgTxt = @MsgTxt + 'cont_stg '+ @con_forename_stg + ', '	
			END
			ELSE
			BEGIN
				SET @MsgTxt = @MsgTxt + 'Inside Owner, '
				SELECT 
					@addressLine1_stg=ISNULL(UPPER(rtrim(ltrim(addressLine1))),''),					
					@addressLine2_stg=ISNULL(UPPER(rtrim(ltrim(addressLine2))),''),
					@addressLine3_stg=ISNULL(UPPER(rtrim(ltrim(addressLine3))),''),
					@addressLine4_stg=ISNULL(UPPER(rtrim(ltrim(addressLine4))),''),
					@addressLine5_stg=ISNULL(UPPER(rtrim(ltrim(addressLine5))),''),
					@addressLine6_stg=ISNULL(UPPER(rtrim(ltrim(addressLine6))),''),
					@addressLine7_stg=ISNULL(UPPER(rtrim(ltrim(addressLine7))),''),
					@addressLine8_stg=ISNULL(UPPER(rtrim(ltrim(addressLine8))),''),
					@addressLine9_stg=ISNULL(UPPER(rtrim(ltrim(addressLine9))),''),
					@addressPostcode_stg=ISNULL(UPPER(rtrim(ltrim(postCode))),''),
					@addressEasting_stg =ISNULL(UPPER(rtrim(ltrim(easting))),''),
					@addressNorthing_stg=ISNULL(UPPER(rtrim(ltrim(northing))),''),
					@addressDesc_stg=ISNULL(UPPER(rtrim(ltrim(description))),''),
					@addressSiteName_stg=ISNULL(UPPER(rtrim(ltrim(siteName))),''),
					@addressCFSID_stg=CFSID
				FROM 
					BT_ADDRESS_MASTER_CRM_STG 
				WITH (NOLOCK)
				WHERE 
					QMS_REF=@pQMSRefNo
				print 'Addr 1'+@addressLine1_stg
				SET @MsgTxt = @MsgTxt + 'addr_stg '+ isnull(@addressLine1_stg,'') + ', '
				--Select contacts from stg table for selected bt message record
				SELECT 
					@con_forename_stg=ISNULL(UPPER(rtrim(ltrim(con_forename))),''),
					@con_surname_stg=ISNULL(UPPER(rtrim(ltrim(con_surname))),''),
					@con_personalTitle_stg=ISNULL(UPPER(rtrim(ltrim(con_personalTitle))),''),
					@con_telephone_stg=ISNULL(UPPER(rtrim(ltrim(con_telephone))),''),
					@con_mobile_stg=ISNULL(UPPER(rtrim(ltrim(con_mobile))),''),
					@con_email_stg=ISNULL(UPPER(rtrim(ltrim(con_email))),''),
					@con_forename_stg_a=ISNULL(UPPER(rtrim(ltrim(con_forename_a))),''),
					@con_surname_stg_a=ISNULL(UPPER(rtrim(ltrim(con_surname_a))),''),
					@con_personalTitle_stg_a=ISNULL(UPPER(rtrim(ltrim(con_personalTitle_a))),''),
					@con_telephone_stg_a=ISNULL(UPPER(rtrim(ltrim(con_telephone_a))),'')
				FROM 
					BT_CONTACT_MASTER_CRM_STG 
				WITH (NOLOCK)
				WHERE 
					QMS_REF=@pQMSRefNo
				SET @MsgTxt = @MsgTxt + 'cont_stg '+ @con_forename_stg + ', '
			END
			SET @MsgTxt = @MsgTxt + 'cont_stg '+ @con_forename_stg + ', '	
			--Check if there is customer exists with selected owner org name, telephone, cust type		
			IF (@cnt > 0)
			BEGIN
				print 'Customer Exists'
				SET @MsgTxt = @MsgTxt + 'Customer Exists, '
				--Select the existing cust id, cust_prim_addrid, CUST_PRIM_CONTACTID 
				SELECT 
					@ccustid=CUST_ID, 
					@ccustPremAddrID=CUST_PRIM_ADDRID, 
					@ccustPremConID=CUST_PRIM_CONTACTID
				FROM 
					BT_CUSTOMER_MASTER CCM 
				WITH (NOLOCK)
				WHERE 
					Ccm.CUST_ORG_NAME=@vOwnerOnly_OrgName 
					AND CCM.CUST_TELEPHONE=@vOwnerOnly_Telephone  
					AND ccm.CUST_TYPE=@vOwnerOnly_Custtype
				
				SET @MsgTxt = @MsgTxt + 'custID '+CONVERT(varchar,@ccustid)+', '
				SET @MsgTxt = @MsgTxt + 'addrID '+CONVERT(varchar,@ccustPremAddrID)+', '
				SET @MsgTxt = @MsgTxt + 'contactID '+CONVERT(varchar,@ccustPremConID)+', '
				--If a valid cust_prim_addrid selected
				IF(@ccustPremAddrID>0)
				BEGIN
					print 'Address Exists'
						--Select address lines to compare the new address is same or not
						--Select all address lines for selected cust_prim_addrid of existing customer
						SELECT 
							@addressLine1=ISNULL(UPPER(rtrim(ltrim(addressLine1))),''),
							@addressLine2=ISNULL(UPPER(rtrim(ltrim(addressLine2))),''),
							@addressLine3=ISNULL(UPPER(rtrim(ltrim(addressLine3))),''),
							@addressLine4=ISNULL(UPPER(rtrim(ltrim(addressLine4))),''),
							@addressLine5=ISNULL(UPPER(rtrim(ltrim(addressLine5))),''),
							@addressLine6=ISNULL(UPPER(rtrim(ltrim(addressLine6))),''),
							@addressLine7=ISNULL(UPPER(rtrim(ltrim(addressLine7))),''),
							@addressLine8=ISNULL(UPPER(rtrim(ltrim(addressLine8))),''),
							@addressLine9=ISNULL(UPPER(rtrim(ltrim(addressLine9))),'')
						FROM 
							BT_ADDRESS_MASTER WITH (NOLOCK) Where ADDRESS_ID=@ccustPremAddrID and deleted != 'Y'
						print 'Addr 1'+@addressLine1
						
				END
								
				--If valid contact Id selected
				IF(@ccustPremConID>0)
				BEGIN
					print 'Contact Exists'
						--Select contacts to compare the new contact is same with the existing contact
						--Select contacts for selected contact id from customer master table
						SELECT 
							@con_forename=     ISNULL(UPPER(rtrim(ltrim(con_forename))),''),
							@con_surname=      ISNULL(UPPER(rtrim(ltrim(con_surname))),''),
							@con_personalTitle=ISNULL(UPPER(rtrim(ltrim(con_personalTitle))),''),
							@con_telephone=    ISNULL(UPPER(rtrim(ltrim(con_telephone))),''),
							@con_mobile=       ISNULL(UPPER(rtrim(ltrim(con_mobile))),''),
							@con_email=        ISNULL(UPPER(rtrim(ltrim(con_email))),'')
						FROM 
							BT_CONTACTS_MASTER 
						WITH (NOLOCK)
						WHERE 
							con_contactID =@ccustPremConID and con_deleted != 'Y'
						
						
				END
			END
			
		SET @MsgTxt = @MsgTxt + 'End select '+CONVERT( VARCHAR(24), GETDATE(), 121)+', '	
		--SET @MsgTxt = @MsgTxt +'*'+ @addressLine1 +' *'+ @addressLine1_stg	
		SET TRANSACTION ISOLATION LEVEL READ UNCOMMITTED
		BEGIN TRAN	
		
				
					
				--Update customer
				--If there is an existing customer, tag the cust id to Enquiry and Estimate
				IF(@cnt>0)
				BEGIN
					SET @MsgTxt = @MsgTxt + 'Cust upd1, '
					UPDATE 
						BT_ENQUIRY_MASTER 
					SET 
						AE_customer=@ccustid 
					WHERE 
						AE_externalRef2=@pQMSRefNo

					UPDATE 
						BT_ESTIMATE_MASTER 
					SET 
						AE_customer=@ccustid 
					WHERE 
						AE_RootID=@ENQAEID
				END
				ELSE --If new customer, update the customer with new contact id, address id, etc ##Changed
				BEGIN--To Do *
					SET @MsgTxt = @MsgTxt + 'Cust upd2, '
					UPDATE  
						BT_CUSTOMER_MASTER
					SET 
						[CUST_ORG_NAME] = @vOwnerOnly_OrgName,[CUST_TELEPHONE] = @vOwnerOnly_Telephone
						,[CUST_PRIM_CONTACTID] = @contactID
						,[CUST_PRIM_ADDRID] = @ae_address
						,[UPDATED_ON] = GETDATE()
						,[UPDATED_BY] = @ploggedonUser
						,[CUST_TYPE] = @vOwnerOnly_Custtype
					WHERE 
						CUST_ID = @vCUST
					/*FROM 
						[BT_CUSTOMER_MASTER] cm
					INNER JOIN 
						BT_ENQUIRY_MASTER enq
					ON 
						enq.AE_customer = cm.cust_id
					INNER JOIN 
						BT_CUSTOMER_MASTER_CRM_stg custg
					ON 
						custg.QMS_REF = enq.AE_externalRef2
					WHERE 
						enq.AE_externalRef2 = @pQMSRefNo*/
					SET @ccustid = @vCUST;
				END
				
				--Update org name in customer name field in BT_Message table
				--To Do Move to last ##Moved to last
				/*UPDATE 
					BT_Message 
				SET 
					Customer_Name=@vOwnerOnly_OrgName
				WHERE 
					QMS_Reference_Number=@pQMSRefNo*/
					
				print 'End of Customer update'
				--Update address
				--If there is an existing customer, and a valid address id found
				IF(@cnt>0)
				BEGIN
					IF(@ccustPremAddrID>0)
					BEGIN
						SET @MsgTxt = @MsgTxt + 'Addr upd1, '
						--SET @MsgTxt = @MsgTxt +'|'+ @addressLine1 +' |'+ @addressLine1_stg
						--Compare selected address lines of existing and new customer 
						IF((@addressLine1=@addressLine1_stg)and(@addressLine2=@addressLine2_stg)AND(@addressLine3=@addressLine3_stg)
						AND(@addressLine4=@addressLine4_stg)AND(@addressLine5=@addressLine5_stg)AND(@addressLine6=@addressLine6_stg)
						AND(@addressLine7=@addressLine7_stg) AND(@addressLine8=@addressLine8_stg)AND(@addressLine9=@addressLine9_stg))
						BEGIN
							SET @MsgTxt = @MsgTxt + 'Addr upd2 '+convert(varchar,@ccustPremAddrID)+', '
							--If address matched, tag the address id to Enquiry and Estimate
							--Select the address ID, which was created in initial load. 
							--The address need to mark as deleted, because same address already exists
							SELECT 
								@ccustPremAddrIDBKP=AE_address,
								@ccustPremConIDBKP=AE_contact 
							FROM 
								BT_ENQUIRY_MASTER 
							WHERE 
								AE_externalRef2=@pQMSRefNo
								
							--Tag the address id to enquiry		
							UPDATE 
								BT_ENQUIRY_MASTER 
							SET 
								AE_address=@ccustPremAddrID
								,AE_CustomerAddress=@ccustPremAddrID
							WHERE 
								AE_externalRef2=@pQMSRefNo
							SET @MsgTxt = @MsgTxt + 'after enq upd '+convert(varchar,@pQMSRefNo)+', '
							--Mark the newly created address as deleted
							UPDATE 
								BT_ADDRESS_MASTER 
							SET 
								deleted='Y' 
							Where 
								ADDRESS_ID=@ccustPremAddrIDBKP
						
							--Update site name in address master
							UPDATE 
								BT_ADDRESS_MASTER 
							SET 
								sitename =(dbo.fn_rpt_GetReformatReturnAddress_NoSiteName(CAST(@ccustPremAddrID AS INT)))
							WHERE 
								ADDRESS_ID=@ccustPremAddrID;

							--Tag the address id to estimate
							UPDATE 
								BT_ESTIMATE_MASTER 
							SET 
								AE_address=@ccustPremAddrID 
							WHERE 
								AE_RootID=@ENQAEID
								
							SET @correspondenceAddrId = @ccustPremAddrID 
							SET @MsgTxt = @MsgTxt + 'after estimate upd '+convert(varchar,@ENQAEID)+', '
						END
						ELSE -- If address not matched, update main table from stg table ##Changed
						BEGIN -- To Do minimise the from statement
							SET @MsgTxt = @MsgTxt + 'Addr upd3, '
							UPDATE  
								BT_ADDRESS_MASTER
							SET  
								[addressLine1] = @addressLine1_stg,[addressLine2] = @addressLine2_stg
								,[addressLine3] = @addressLine3_stg,[addressLine4] = @addressLine4_stg
								,[addressLine5] = @addressLine5_stg ,[addressLine6] = @addressLine6_stg
								,[addressLine7] = @addressLine7_stg,[addressLine8] = @addressLine8_stg
								,[addressLine9] = @addressLine9_stg,[postCode] = @addressPostcode_stg,
								[Country] = 1,[easting] = @addressEasting_stg,[Northing] = @addressNorthing_stg
								,[description] = @addressDesc_stg,[CFSID] = @addressCFSID_stg,
								[siteName] = @addressSitename_stg
								,lastUpdated = GETDATE()
								,lastUpdatedBy = @ploggedonUser,
								addressUsage=13,
								parentid = @ccustid,
								addr_contactName = CONCAT(@con_personalTitle_stg,' ',@con_forename_stg,' ',@con_surname_stg)
							WHERE
								ADDRESS_ID = @ae_address -- To Do ##Changed
							/*FROM 
								[BT_CUSTOMER_MASTER] cm
							INNER JOIN 
								BT_ENQUIRY_MASTER enq
							ON 
								enq.AE_customer = cm.cust_id
							INNER JOIN 
								BT_ADDRESS_MASTER addr
							ON 
								addr.parentID = cm.CUST_ID
							INNER JOIN 
								BT_ADDRESS_MASTER_CRM_stg addrestg
							ON 
								addrestg.QMS_REF = enq.AE_externalRef2
							WHERE 
								enq.AE_externalRef2 = @pQMSRefNo*/
								
								
				 
							--MN: 26082021 SET CONCATINATED SITE NAME INTO SITE ADDRESS DETAILS
							--SET @vAEAddress =(SELECT TOP 1 AE_Address from BT_ENQUIRY_MASTER WHERE AE_externalRef2 = @pQMSRefNo)

							--Update site name in address master
							UPDATE 
								BT_ADDRESS_MASTER 
							SET 
								sitename =dbo.fn_rpt_GetReformatReturnAddress_NoSiteName(@ae_address)
							WHERE 
								ADDRESS_ID=@ae_address; --##Changed
													
							--Select address id to update in customer master ** don't know the purpose of update ##Changed
							--To Do Minimise the from statement
							/*SELECT 
								@vAddressId= LTRIM(RTRIM(addr.ADDRESS_ID))
								,@vCust_ID=LTRIM(RTRIM(cm.CUST_ID))
							FROM 
								[BT_CUSTOMER_MASTER] cm
							INNER JOIN 
								BT_ENQUIRY_MASTER enq
							ON 
								enq.AE_customer = cm.cust_id
							INNER JOIN 
								BT_ADDRESS_MASTER addr
							ON 
								addr.parentID = cm.CUST_ID
							INNER JOIN 
								BT_ADDRESS_MASTER_CRM_stg addrestg
							ON 
								addrestg.QMS_REF = enq.AE_externalRef2
							WHERE 
								enq.AE_externalRef2 = @pQMSRefNo 
								AND (addr.addressUsage=13 OR addr.addressUsage=1)*/
							
								
							--Update address id in customer master
							UPDATE 
								[BT_CUSTOMER_MASTER] 
							SET 
								CUST_PRIM_ADDRID=@ae_address 
							WHERE 
								CUST_ID=@vCUST
						END
					END
					ELSE -- If address id <= 0
					BEGIN -- To do  ##Changed
						SET @MsgTxt = @MsgTxt + 'Addr upd4, '
						UPDATE  
							BT_ADDRESS_MASTER
						SET  
							[addressLine1] = @addressLine1_stg,[addressLine2] = @addressLine2_stg
							,[addressLine3] = @addressLine3_stg,[addressLine4] = @addressLine4_stg
							,[addressLine5] = @addressLine5_stg ,[addressLine6] = @addressLine6_stg
							,[addressLine7] = @addressLine7_stg,[addressLine8] = @addressLine8_stg
							,[addressLine9] = @addressLine9_stg,[postCode] = @addressPostcode_stg,
							[Country] = 1,[easting] = @addressEasting_stg,[Northing] = @addressNorthing_stg
							,[description] = @addressDesc_stg,[CFSID] = @addressCFSID_stg,
							[siteName] = @addressSitename_stg
							,lastUpdated = GETDATE()
							,lastUpdatedBy = @ploggedonUser,
							addressUsage=13,
							parentid = @ccustid,
							addr_contactName = CONCAT(@con_personalTitle_stg,' ',@con_forename_stg,' ',@con_surname_stg)				
						WHERE
							ADDRESS_ID = @ae_address
						/*FROM 
							[BT_CUSTOMER_MASTER] cm
						INNER JOIN 
							BT_ENQUIRY_MASTER enq
						ON 
							enq.AE_customer = cm.cust_id
						INNER JOIN 
							BT_ADDRESS_MASTER addr
						ON 
							addr.parentID = cm.CUST_ID
						INNER JOIN 
							BT_ADDRESS_MASTER_CRM_stg addrestg
						ON 
							addrestg.QMS_REF = enq.AE_externalRef2
						WHERE 
							enq.AE_externalRef2 = @pQMSRefNo*/
				 
						--MN: 26082021 SET CONCATINATED SITE NAME INTO SITE ADDRESS DETAILS
						--SET @vAEAddress =(SELECT TOP 1 AE_Address from BT_ENQUIRY_MASTER WHERE AE_externalRef2 = @pQMSRefNo)

						--Update site name in address master
						UPDATE 
							BT_ADDRESS_MASTER 
						SET 
							sitename =dbo.fn_rpt_GetReformatReturnAddress_NoSiteName(@ae_address)
						WHERE 
							ADDRESS_ID=@ae_address;
												
						--Select address id to update in customer master ** don't know the purpose of update
						/*SELECT 
							@vAddressId= LTRIM(RTRIM(addr.ADDRESS_ID))
							,@vCust_ID=LTRIM(RTRIM(cm.CUST_ID)) 
						FROM 9
							[BT_CUSTOMER_MASTER] cm
						INNER JOIN 
							BT_ENQUIRY_MASTER enq
						ON 
							enq.AE_customer = cm.cust_id
						INNER JOIN 
							BT_ADDRESS_MASTER addr
						ON 
							addr.parentID = cm.CUST_ID
						INNER JOIN 
							BT_ADDRESS_MASTER_CRM_stg addrestg
						ON 
							addrestg.QMS_REF = enq.AE_externalRef2
						WHERE 
							enq.AE_externalRef2 = @pQMSRefNo 
							AND (addr.addressUsage=13 OR addr.addressUsage=1)*/
						

						--Update address id in customer master
						UPDATE 
							[BT_CUSTOMER_MASTER] 
						SET 
							CUST_PRIM_ADDRID=@ae_address 
						WHERE 
							CUST_ID=@vCUST
					END
				END
				ELSE -- If there is no existing customer, update address master from stg table
				BEGIN -- To Do ##Changed
					SET @MsgTxt = @MsgTxt + 'Addr upd5, '
					UPDATE  
						BT_ADDRESS_MASTER
					SET  
						[addressLine1] = @addressLine1_stg,[addressLine2] = @addressLine2_stg
						,[addressLine3] = @addressLine3_stg,[addressLine4] = @addressLine4_stg
						,[addressLine5] = @addressLine5_stg ,[addressLine6] = @addressLine6_stg
						,[addressLine7] = @addressLine7_stg,[addressLine8] = @addressLine8_stg
						,[addressLine9] = @addressLine9_stg,[postCode] = @addressPostcode_stg,
						[Country] = 1,[easting] = @addressEasting_stg,[Northing] = @addressNorthing_stg
						,[description] = @addressDesc_stg,[CFSID] = @addressCFSID_stg,
						[siteName] = @addressSitename_stg
						,lastUpdated = GETDATE()
						,lastUpdatedBy = @ploggedonUser,
						addressUsage=13,
						parentid = @ccustid,
						addr_contactName = CONCAT(@con_personalTitle_stg,' ',@con_forename_stg,' ',@con_surname_stg)
					WHERE
						ADDRESS_ID = @ae_address
					/*FROM 
						[BT_CUSTOMER_MASTER] cm
					INNER JOIN 
						BT_ENQUIRY_MASTER enq
					ON 
						enq.AE_customer = cm.cust_id
					INNER JOIN 
						BT_ADDRESS_MASTER addr
					ON 
						addr.parentID = cm.CUST_ID
					INNER JOIN 
						BT_ADDRESS_MASTER_CRM_stg addrestg
					ON 
						addrestg.QMS_REF = enq.AE_externalRef2
					WHERE 
						enq.AE_externalRef2 = @pQMSRefNo*/
				 
					--MN: 26082021 SET CONCATINATED SITE NAME INTO SITE ADDRESS DETAILS
					--SET @vAEAddress =(SELECT TOP 1 AE_Address from BT_ENQUIRY_MASTER WHERE AE_externalRef2 = @pQMSRefNo)

					--update site name in address master
					UPDATE 
						BT_ADDRESS_MASTER 
					SET 
						sitename =dbo.fn_rpt_GetReformatReturnAddress_NoSiteName(@ae_address)
					WHERE 
						ADDRESS_ID=@ae_address;
												
					--Select address id to update in customer master ** Don't know the purpose of the update
					/*SELECT 
						@vAddressId= LTRIM(RTRIM(addr.ADDRESS_ID))
						,@vCust_ID=LTRIM(RTRIM(cm.CUST_ID))
					FROM 
						[BT_CUSTOMER_MASTER] cm
					INNER JOIN 
						BT_ENQUIRY_MASTER enq
					ON 
						enq.AE_customer = cm.cust_id
					INNER JOIN 
						BT_ADDRESS_MASTER addr
					ON 
						addr.parentID = cm.CUST_ID
					INNER JOIN 
						BT_ADDRESS_MASTER_CRM_stg addrestg
					ON 
						addrestg.QMS_REF = enq.AE_externalRef2
					WHERE 
						enq.AE_externalRef2 = @pQMSRefNo 
						AND (addr.addressUsage=13 OR addr.addressUsage=1)*/
					

					--Update address id in customer master
					UPDATE 
						[BT_CUSTOMER_MASTER] 
					SET 
						CUST_PRIM_ADDRID=@ae_address 
					WHERE 
						CUST_ID=@vCUST
				END
		print 'End of address update'
				--Update contact
				--If customer exists and a valid contact id found
				IF(@cnt>0)
				BEGIN
					IF(@ccustPremConID>0)
					BEGIN
						SET @MsgTxt = @MsgTxt + 'Cnt upd1, '
						--Compare selected contacts (Existing and new)
						IF((@con_forename = @con_forename_stg)AND(@con_surname=@con_surname_stg)
						AND(@con_telephone=@con_telephone_stg)AND(@con_mobile=@con_mobile_stg)
						AND(@con_email=@con_email_stg))
						BEGIN
							SET @MsgTxt = @MsgTxt + 'Cnt upd2, '
							--Contacts matched! 
							--Select the contact id which was created in initial load to mark as deleted
							SELECT 
								@ccustPremConIDBKP=AE_contact 
							FROM 
								BT_ENQUIRY_MASTER 
							WHERE 
								AE_externalRef2=@pQMSRefNo
							
							--Tag the existing contact id in enquiry
							UPDATE 
								BT_ENQUIRY_MASTER 
							SET 
								AE_contact=CAST(@ccustPremConID AS varchar(50))
							WHERE 
								AE_externalRef2=@pQMSRefNo
									
							--Mark newly created contcat as deleted
							UPDATE 
								BT_CONTACTS_MASTER 
							SET 
								con_deleted='Y' 
							WHERE 
								con_contactID=@ccustPremConIDBKP

							--Tag the existing contact id in estimate
							UPDATE 
								BT_ESTIMATE_MASTER 
							SET 
								AE_contact=CAST(@ccustPremConID AS varchar(50)) 
							WHERE 
								AE_RootID=@ENQAEID
						END
						ELSE --If Contact not matched 
						BEGIN
							SET @MsgTxt = @MsgTxt + 'Cnt upd3, '
							--For domestic contact, update contact from stg to main table ##Changed
							IF  @vdomesticorCommercial = 'domestic'
							BEGIN
								SET @MsgTxt = @MsgTxt + 'Cnt upd4, '
								UPDATE  
									BT_CONTACTS_MASTER
								SET 
									[con_surname] = @con_surname_stg,
									[con_forename] = @con_forename_stg
									,[con_personalTitle] = @con_personalTitle_stg ,
									[con_telephone] = @con_telephone_stg
									,[con_mobile] = @con_mobile_stg,
									[con_email] = @con_email_stg
									,con_lastUpdated = GETDATE()
									,con_lastUpdatedBy = @ploggedonUser,
									con_contactName=CONCAT(@con_personalTitle_stg,' ',@con_forename_stg,' ',@con_surname_stg),
									con_parentid = @ccustid
								WHERE
									con_contactID = @contactID
										/*FROM 
											[BT_CUSTOMER_MASTER] cm
										INNER JOIN 
											BT_ENQUIRY_MASTER enq
										ON 
											enq.AE_customer = cm.cust_id
										INNER JOIN 
											BT_CONTACTS_MASTER con
										ON 
											con.con_parentID = cm.CUST_ID
										INNER JOIN 
											BT_CONTACT_MASTER_CRM_STG constg
										ON 
											constg.QMS_REF = enq.AE_externalRef2
										WHERE 
											enq.AE_externalRef2 = @pQMSRefNo*/
							END
							ELSE -- For non domestic contact (Commercial), update contact master from stg
							BEGIN
								SET @MsgTxt = @MsgTxt + 'Cnt upd5, '
								UPDATE  
									BT_CONTACTS_MASTER
								SET 
									[con_surname] = @vOwnerOnly_OrgName,
									[con_forename] = 'Commercial'
									,[con_personalTitle] = '' ,
									[con_telephone] = @con_telephone_stg
									,[con_mobile] = @con_mobile_stg,
									[con_email] = @con_email_stg
									,con_lastUpdated = GETDATE()
									,con_lastUpdatedBy = @ploggedonUser,
									con_contactName=CONCAT(@con_personalTitle_stg,' '
									,@con_forename_stg,' ',@con_surname_stg),
									con_parentid = @ccustid
								WHERE
									con_contactID = @contactID
										/*FROM 
											[BT_CUSTOMER_MASTER] cm
										INNER JOIN 
											BT_ENQUIRY_MASTER enq
										ON 
											enq.AE_customer = cm.cust_id
										INNER JOIN 
											BT_CUSTOMER_MASTER_stg custg
										ON 
											custg.QMS_REF = enq.AE_externalRef2
										INNER JOIN 
											BT_CONTACTS_MASTER con
										ON 
											con.con_parentID = cm.CUST_ID
										INNER JOIN 
											BT_CONTACT_MASTER_CRM_STG constg
										ON 
											constg.QMS_REF = enq.AE_externalRef2
										WHERE 
											enq.AE_externalRef2 = @pQMSRefNo*/
							END
						END
					END
				END
				ELSE --If existing customer not found
				BEGIN
					
					--For domestic contact, update contact master from stg
					IF  @vdomesticorCommercial = 'domestic'
					BEGIN
						SET @MsgTxt = @MsgTxt + 'Cnt upd6, '
						UPDATE  
							BT_CONTACTS_MASTER
						SET 
							[con_surname] = @con_surname_stg,
							[con_forename] = @con_forename_stg
							,[con_personalTitle] = @con_personalTitle_stg ,
							[con_telephone] = @con_telephone_stg
							,[con_mobile] = @con_mobile_stg,
							[con_email] = @con_email_stg
							,con_lastUpdated = GETDATE()
							,con_lastUpdatedBy = @ploggedonUser,
							con_contactName=CONCAT(@con_personalTitle_stg,' ',@con_forename_stg,' ',@con_surname_stg),
							con_parentid = @ccustid
						WHERE
							con_contactID = @contactID
							/*FROM 
								[BT_CUSTOMER_MASTER] cm
							INNER JOIN 
								BT_ENQUIRY_MASTER enq
							ON 
								enq.AE_customer = cm.cust_id
							INNER JOIN 
								BT_CONTACTS_MASTER con
							ON 
								con.con_parentID = cm.CUST_ID
							INNER JOIN 
								BT_CONTACT_MASTER_CRM_STG constg
							ON 
								constg.QMS_REF = enq.AE_externalRef2
							WHERE 
								enq.AE_externalRef2 = @pQMSRefNo*/
					END
					ELSE -- For non domestic contact(commercial), update contact from stg
					BEGIN
						SET @MsgTxt = @MsgTxt + 'Cnt upd7, '
						UPDATE  
							BT_CONTACTS_MASTER
						SET 
							[con_surname] = @vOwnerOnly_OrgName,
							[con_forename] = 'Commercial'
							,[con_personalTitle] = '' ,
							[con_telephone] = @con_telephone_stg
							,[con_mobile] = @con_mobile_stg,
							[con_email] = @con_email_stg
							,con_lastUpdated = GETDATE()
							,con_lastUpdatedBy = @ploggedonUser,
							con_contactName=CONCAT(@con_personalTitle_stg,' '
							,@con_forename_stg,' ',@con_surname_stg),
							con_parentid = @ccustid
						WHERE
							con_contactID = @contactID
							/*FROM 
								[BT_CUSTOMER_MASTER] cm
							INNER JOIN 
								BT_ENQUIRY_MASTER enq
							ON 
								enq.AE_customer = cm.cust_id
							INNER JOIN 
								BT_CUSTOMER_MASTER_stg custg
							ON 
								custg.QMS_REF = enq.AE_externalRef2
							INNER JOIN 
								BT_CONTACTS_MASTER con
							ON 
								con.con_parentID = cm.CUST_ID
							INNER JOIN 
								BT_CONTACT_MASTER_CRM_STG constg
							ON 
								constg.QMS_REF = enq.AE_externalRef2
							WHERE 
								enq.AE_externalRef2 = @pQMSRefNo*/
					END
				END
				print 'End of Contact update'
				--Update Enquiry
				UPDATE  
					enq
			    SET 
					[AE_dateOrigin] = CAST(enqstg.AE_CompliantInfoReceiveDate AS SMALLDATETIME),
					[AE_orderReceivedDate] = CAST(enqstg.AE_dateOrigin AS SMALLDATETIME)
				    ,[AE_externalRef] = enqstg.AE_externalRef
					,[AE_summaryDescription] = enqstg.AE_summaryDescription
					--,AE_CustomerAddress = @vCustomerAddressId
					,AE_status = (select LOV_VALUE from MT_SYS_LOV
									where LOV_DESC='Major Works Live' 
									AND LOV_NAME='sysStatusEnquiry')					
					,[AE_type] = (SELECT LOV_VALUE FROM MT_SYS_LOV WHERE rtrim(ltrim(coalesce(LOV_Name,'')))
								='actEnquiryType' AND LTRIM(RTRIM(UPPER(LOV_DESC)))=
							LTRIM(RTRIM(UPPER(enqstg.[Source]))) AND LOV_DELETED=0)				 
					,[AE_CustType] =
					(SELECT TOP 1 CustomerType_QPID FROM customerType_QPID_QMS_Map 
						WHERE CustomerType_QMS= enqstg.AE_CustType)				  
					,[AE_assignedToDept] = @vDepartment_Qpid_value
					,[AE_assignedToPerson] = @assignToPersonID
					,[AE_plotUnit] = enqstg.AE_plotUnit
					,[Source] = NULL
					,[AE_networkDistributor] =(SELECT TOP 1 LOV_VALUE 
							FROM MT_SYS_LOV 
							WHERE UPPER(rtrim(ltrim(coalesce(LOV_NAME,''))))='SYS_COMPANY' 
							AND LTRIM(RTRIM(UPPER(LOV_DESC)))=RTRIM(LTRIM(UPPER(enqstg.AE_networkDistributor))) AND LOV_DELETED=0)
				    ,AE_serviceType = 
				    (CASE WHEN UPPER(LTRIM(RTRIM(enqstg.AE_serviceType)))=UPPER(LTRIM(RTRIM('Single Phase')))
					THEN 1 
					WHEN UPPER(LTRIM(RTRIM(enqstg.AE_serviceType)))=UPPER(LTRIM(RTRIM('Two Phase')))
					THEN 2
					WHEN UPPER(LTRIM(RTRIM(enqstg.AE_serviceType)))=UPPER(LTRIM(RTRIM('Three Phase')))
					THEN 2
					WHEN UPPER(LTRIM(RTRIM(enqstg.AE_serviceType)))=UPPER(LTRIM(RTRIM('Split Phase')))
					THEN 2
					ELSE 0 END)																																
					,[AE_ExternalRef3] = (CASE 
					WHEN UPPER(LTRIM(RTRIM(enqstg.AE_ExternalRef3)))=UPPER(LTRIM(RTRIM('Owner'))) THEN 'Owner/Occupier' 
					WHEN UPPER(LTRIM(RTRIM(enqstg.AE_ExternalRef3)))=UPPER(LTRIM(RTRIM('Agent'))) THEN 'Agent' ELSE enqstg.AE_ExternalRef3 END)
					,[AE_QuoteProjectDiscuss] = (SELECT LOV_VALUE FROM MT_SYS_LOV WHERE rtrim(ltrim(coalesce(LOV_Name,'')))='sysQuoteProjectDiscuss' AND LOV_DELETED=0  AND ISNULL(LOV_OBSOLETE,'N') <> 'Y' AND  UPPER(LTRIM(RTRIM(LOV_DESC)))= UPPER(LTRIM(RTRIM(enqstg.discussedQuote_project))))/*(CASE 
					WHEN UPPER(LTRIM(RTRIM(enqstg.discussedQuote_project)))='EMAIL' THEN 'Email' 
					WHEN UPPER(LTRIM(RTRIM(enqstg.discussedQuote_project)))='LETTER' THEN 'Letter' 
					WHEN UPPER(LTRIM(RTRIM(enqstg.discussedQuote_project)))='EMAIL AND LETTER' THEN 'ALL' 
					WHEN UPPER(LTRIM(RTRIM(enqstg.discussedQuote_project)))='TELEPHONE' THEN 'Phone' 
					WHEN UPPER(LTRIM(RTRIM(enqstg.discussedQuote_project)))='PHONE' THEN 'Phone' ELSE '0' END)*/
					,[AE_QuoteProvision] = (SELECT LOV_VALUE FROM MT_SYS_LOV WHERE rtrim(ltrim(coalesce(LOV_Name,'')))='sysQuoteProvision' AND LOV_DELETED=0  AND ISNULL(LOV_OBSOLETE,'N') <> 'Y' AND  UPPER(LTRIM(RTRIM(LOV_DESC)))= UPPER(LTRIM(RTRIM(enqstg.provisionofQuote))))/*(CASE 
					WHEN UPPER(LTRIM(RTRIM(enqstg.provisionofQuote)))='EMAIL' THEN 'Email'
					WHEN UPPER(LTRIM(RTRIM(enqstg.provisionofQuote)))='LETTER' THEN 'Letter'
					WHEN UPPER(LTRIM(RTRIM(enqstg.provisionofQuote)))='EMAIL AND LETTER' THEN 'Both' ELSE '0' END)*/
					,AE_generationType = (CASE WHEN UPPER(LTRIM(RTRIM(enqstg.AE_GenerationInvolved)))=UPPER(LTRIM(RTRIM('Y')))
					THEN 1 
					WHEN UPPER(LTRIM(RTRIM(enqstg.AE_GenerationInvolved)))=UPPER(LTRIM(RTRIM('N')))
					THEN 2 ELSE 0 END),
					AE_quoteDesigner=@QuoteDesignerId
					,[AE_WorkType] = (SELECT LOV_VALUE FROM MT_SYS_LOV WHERE rtrim(ltrim(coalesce(LOV_Name,'')))
								='sysWorkType' AND LTRIM(RTRIM(UPPER(LOV_DESC)))='MAJOR WORKS' AND LOV_DELETED=0)		
				FROM 
					/*[BT_CUSTOMER_MASTER] cm
				INNER JOIN 
					BT_ENQUIRY_MASTER enq
				ON 
					enq.AE_customer = cm.cust_id
				INNER JOIN 
					BT_ENQUIRY_MASTER_CRM_stg enqstg
				ON 
					enqstg.QMS_REF = enq.AE_externalRef2
				WHERE 
					enq.AE_externalRef2 = @pQMSRefNo*/ --##Changed
					BT_ENQUIRY_MASTER enq
				INNER JOIN
					BT_ENQUIRY_MASTER_CRM_stg enqstg
				ON 
					enqstg.QMS_REF = enq.AE_externalRef2
				WHERE
					enq.AE_AEID = @ENQAEID 
					AND enqstg.QMS_REF = @pQMSRefNo

				--Need to add the history Addition stored proedure for Enquiry Edit
				--MN: 26082021 UPDATEING MT_ENQUIRY_QUESTIONS FOR GEN INVOLVE AND TYPE OF SERVICES
				UPDATE 
					MT_ENQUIRY_QUESTIONS 
				SET 
					aeq_HA_Voltage_LOV = 1,
					aeq_GENERATION_LOV = (CASE 
					WHEN UPPER(LTRIM(RTRIM(enqstg.AE_GenerationInvolved)))='Y' THEN 1 
					WHEN UPPER(LTRIM(RTRIM(enqstg.AE_GenerationInvolved)))='N' THEN 2 ELSE 0 END),
					[aeq_TOW_REQUIRED_LOV]=(CASE 
					WHEN UPPER(LTRIM(RTRIM(enqstg.AE_serviceType)))='SINGLE PHASE' THEN 1 ELSE 2 END)
				FROM 
					MT_ENQUIRY_QUESTIONS que
				INNER JOIN 
					BT_ENQUIRY_MASTER enq
				ON 
					enq.AE_AEID=que.aeq_ae_aeid
				INNER JOIN 
					BT_ENQUIRY_MASTER_CRM_stg enqstg
				ON 
					enqstg.QMS_REF = enq.AE_externalRef2
				WHERE 
					enq.AE_externalRef2 = @pQMSRefNo 
					AND enq.AE_AEID = @ENQAEID 
				print 'End of Enquiry update'
				--Begin INSERT
				
				--Generate new address id for insert
				SELECT @vAddIdA = MAX(ADDRESS_ID)+1 FROM BT_ADDRESS_MASTER;
				SET @MsgTxt = @MsgTxt + 'agnt_addr '+ CONVERT(VARCHAR, @vAddIdA) + ', '
				--Insert address for enquiry    
				INSERT INTO 
					BT_ADDRESS_MASTER  
				   ([ADDRESS_ID],[parentID], [addressLine1],[addressLine2], [addressLine3]  
				   ,[addressLine4],[addressLine5] ,[addressLine6],[addressLine7] 
				   ,[addressLine8],[addressLine9] ,[postCode],[easting] ,[Northing] ,
				    [addressUsage],
				   [dateCreated]  ,[createdBy],[lastUpdated],[lastUpdatedBy] ,[CFSID],[deleted],[SOURCE_SYS],[addr_contactTel1],[addr_contactName])
				SELECT   
					@vAddIdA,@ENQAEID,addrestg.[addressLine1_A],addrestg.[addressLine2_A],addrestg.[addressLine3_A]  
				   ,addrestg.[addressLine4_A],addrestg.[addressLine5_A] ,addrestg.[addressLine6_A],addrestg.[addressLine7_A] 
				   ,addrestg.[addressLine8_A],addrestg.[addressLine9_A] ,addrestg.[postCode_A],addrestg.[easting_A] ,addrestg.[Northing_A] ,
				   0, 
				   GETDATE()  ,@ploggedonUser,GETDATE(),@ploggedonUser ,addrestg.[CFSID]_A,'N','CRM',@con_telephone_stg_a,
				   CONCAT(@con_personalTitle_stg_a,' ',@con_forename_stg_a,' ',@con_surname_stg_a)
				FROM 
					BT_ADDRESS_MASTER_CRM_stg addrestg		
				WHERE 
					addrestg.QMS_REF = @pQMSRefNo
					/*BT_ADDRESS_MASTER_stg addrestg
				INNER JOIN 
					BT_CONTACT_MASTER_CRM_STG constg
				ON 
					addrestg.QMS_REF=constg.QMS_REF
				WHERE 
					addrestg.QMS_REF = @pQMSRefNo*/
		
	
				--Generate ID for CDMS_ACT_AE_EXTRA
				DECLARE @EX_ID INT 
				Select @EX_ID=MAX(EX_ID)+1 from CDMS_ACT_AE_EXTRA 
		
				--Insert in CDMS_ACT_AE_EXTRA
				INSERT INTO 
					[dbo].[CDMS_ACT_AE_EXTRA]
					([EX_ID],[EX_AEID],[EX_clawBackAmount],[EX_dateCreated],[EX_dateCreatedBy],[EX_lastUpdated],[EX_lastUpdatedBy], 
					[EX_deleted],[EX_PO_RefNumber],[EX_altContactAddress])
				VALUES
					(@EX_ID,@ENQAEID,0,GETDATE(),@ploggedonUser,GETDATE(),@ploggedonUser,'N',NULL,@vAddIdA)
		
				/*Insert Site Address*/
				--Generate address id for insert
				SELECT @vAddIdS = MAX(ADDRESS_ID)+1 FROM BT_ADDRESS_MASTER; 
				--SELECT @vAddIdS = @vAddIdA+1    
				SET @MsgTxt = @MsgTxt + 'site_addr_id '+ CONVERT(VARCHAR,@vAddIdS) + ', '
                --Insert address for customer
				INSERT INTO 
					BT_ADDRESS_MASTER  
					([ADDRESS_ID],[parentID], [addressLine1],[addressLine2], [addressLine3]  
					,[addressLine4],[addressLine5] ,[addressLine6],[addressLine7] 
					,[addressLine8],[addressLine9] ,[postCode],[easting] ,[Northing] ,
					[addressUsage] , 
					[dateCreated]  ,[createdBy],[lastUpdated],[lastUpdatedBy] ,[CFSID],[deleted],SOURCE_SYS)
				SELECT   
					@vAddIdS,@vCUST,addrestg.[addressLine1_S],addrestg.[addressLine2_S],addrestg.[addressLine3_S] 
					,addrestg.[addressLine4_S],addrestg.[addressLine5_S],addrestg.[addressLine6_S],addrestg.[addressLine7_S]
					,addrestg.[addressLine8_S],addrestg.[addressLine9_S],addrestg.[postCode_S],addrestg.[easting_S] ,addrestg.[Northing_S] ,
					2,  
					GETDATE()  ,@ploggedonUser,GETDATE(),@ploggedonUser ,addrestg.[CFSID_S],'N','CRM'
				FROM   
					BT_ADDRESS_MASTER_CRM_stg addrestg		
				WHERE 
					addrestg.QMS_REF = @pQMSRefNo
       
				--Update the customer address id in enquiry master
				UPDATE 
					BT_enquiry_master 
				SET 
					AE_address=@vAddIdS 
				WHERE 
					AE_externalRef2=@pQMSRefNo
					
				--Update site name in address master	
				UPDATE 
					BT_ADDRESS_MASTER 
				SET 
					sitename =dbo.fn_rpt_GetReformatReturnAddress_NoSiteName(@vAddIdS)
				WHERE 
					ADDRESS_ID=@vAddIdS;	
				
				/*Insert Agent Contact --*/
				--Generate contact id for insert
				SELECT @vConId = MAX(con_contactID)+1 FROM BT_CONTACTS_MASTER; 
				
				--Insert contact for domestic or commercial
				IF  @vdomesticorCommercial_A = 'domestic'
				BEGIN
					SET @MsgTxt = @MsgTxt + 'Cnt ins1, '
					INSERT INTO 
						BT_CONTACTS_MASTER  
						([con_contactID],[con_parentID], [con_parentType],[con_surname] ,[con_forename]  
						,[con_personalTitle]
						,[con_telephone] ,[con_email] ,[con_mobile] ,[con_dateCreated],[con_createdBy],
						[con_lastUpdated], [con_lastUpdatedBy],[con_deleted]
						,con_contactName,SOURCE_SYS  
						)  
					SELECT   
						@vConId,@vCUST,1,constg.[con_surname_A] ,constg.[con_forename_A]  
						,constg.[con_personalTitle_A] ,constg.[con_telephone_A]  
						,constg.[con_email_A] ,constg.[con_mobile_A] ,GETDATE(),@ploggedonUser,GETDATE(),  
						@ploggedonUser,'N',
						CONCAT(constg.[con_personalTitle_A],' ',constg.[con_forename_A],' ',constg.[con_surname_A]),'CRM'
					FROM   
						BT_CONTACT_MASTER_CRM_STG constg	
					WHERE 
						constg.QMS_REF = @pQMSRefNo
				END
				ELSE
				BEGIN
					SET @MsgTxt = @MsgTxt + 'Cnt ins2, '
					INSERT INTO 
						BT_CONTACTS_MASTER  
						([con_contactID],[con_parentID], [con_parentType],[con_surname] ,[con_forename]  
						,[con_personalTitle]
						,[con_telephone] ,[con_email] ,[con_mobile] ,[con_dateCreated],[con_createdBy],
						[con_lastUpdated], [con_lastUpdatedBy],[con_deleted]
						,con_contactName ,SOURCE_SYS 
						)  
					SELECT   
						@vConId,@vCUST,1,custg.CUST_ORG_NAME ,'Commercial'  
						,'',constg.[con_telephone_A]  
						,constg.[con_email_A] ,constg.[con_mobile_A] ,GETDATE(),@ploggedonUser,GETDATE(),  
						@ploggedonUser,'N'
						,CONCAT(constg.[con_personalTitle_A],' ',constg.[con_forename_A],' ',constg.[con_surname_A]),'CRM'
					FROM   
						BT_CONTACT_MASTER_CRM_STG constg	
					INNER JOIN 
						BT_CUSTOMER_MASTER_CRM_STG custg
					ON 
						constg.QMS_REF = custg.QMS_REF
					WHERE 
						constg.QMS_REF = @pQMSRefNo
				END
			
				/*Agent Owner ocuupier*/
				
				--Select customer address id 
				/*SELECT @vOwnerOccupiedAddrId = addr.ADDRESS_ID 
				FROM BT_CUSTOMER_MASTER cm
				INNER JOIN [dbo].BT_ENQUIRY_MASTER enq
				ON enq.AE_customer = cm.CUST_ID
				INNER JOIN BT_ADDRESS_MASTER addr
				ON addr.parentID = cm.CUST_ID
				WHERE enq.AE_AEID = @ENQAEID AND (addr.addressUsage=1 OR addr.addressUsage=13)*/

				/*IF UPPER(LTRIM(RTRIM(@vAE_ExternalRef3))) = 'OWNER'
				BEGIN
					SET @MsgTxt = @MsgTxt + 'Corr addr upd1, '
					SET @vAddIdO = @vOwnerOccupiedAddrId
					--Update address id in enquiry master
					UPDATE 
						act
					SET 
						act.[AE_correspondenceAddress] = @vAddIdO
					FROM 
						[dbo].BT_ENQUIRY_MASTER act
					WHERE 
						act.AE_AEID = @ENQAEID
				END
				ELSE
				BEGIN
					SET @MsgTxt = @MsgTxt + 'Corr addr upd2, '
					SET @vAddIdO = @vAddIdA
					--Update address id in CDMS_ACT_AE_EXTRA for agent
					UPDATE 
						extra
					SET 
						extra.EX_altContactAddress  = @vAddIdO
					FROM 
						[dbo].CDMS_ACT_AE_EXTRA extra
					WHERE 
						extra.EX_AEID = @ENQAEID
				END*/
				DECLARE @billingAddrID BIGINT;
				IF UPPER(LTRIM(RTRIM(@vAE_ExternalRef3))) = 'OWNER'
				BEGIN
					
					SET @vAddIdO = @correspondenceAddrId
					SET @MsgTxt = @MsgTxt + 'Corr addr upd1 '+convert(varchar,@vAddIdO)+', '
					--Update address id in enquiry master
					UPDATE 
						act
					SET 
						act.[AE_correspondenceAddress] = @vAddIdO
					FROM 
						[dbo].BT_ENQUIRY_MASTER act
					WHERE 
						act.AE_AEID = @ENQAEID
						
					SELECT @billingAddrID = MAX(ADDRESS_ID)+1 FROM BT_ADDRESS_MASTER;
					SET @MsgTxt = @MsgTxt + 'Billing address id  '+ CONVERT(VARCHAR, @billingAddrID) + ', '
					--Insert billing address    
					INSERT INTO 
						BT_ADDRESS_MASTER  
						([ADDRESS_ID],[parentID], [addressLine1],[addressLine2], [addressLine3]  
						,[addressLine4],[addressLine5] ,[addressLine6],[addressLine7] 
						,[addressLine8],[addressLine9]  ,[postCode],[easting] ,[Northing] ,
						[addressUsage],
						[dateCreated]  ,[createdBy],[lastUpdated],[lastUpdatedBy] ,[CFSID],[deleted],[SOURCE_SYS],[addr_contactTel1],[addr_contactName])
					SELECT   
						@billingAddrID,@ENQAEID,addrestg.[Company_Name_B],addrestg.[addressLine2_B],addrestg.[addressLine3_B]  
						,addrestg.[addressLine4_B],addrestg.[addressLine5_B] ,addrestg.[addressLine6_B],addrestg.[addressLine7_B] 
						,addrestg.[addressLine8_B],addrestg.[addressLine9_B] ,addrestg.[postCode_B],addrestg.[easting_B] ,addrestg.[Northing_B] ,
						1, 
						GETDATE()  ,@ploggedonUser,GETDATE(),@ploggedonUser ,addrestg.[CFSID]_B,'N','CRM',addrestg.[Email_B],
						CONCAT(addrestg.[Title_B],' ',@con_forename_stg,' ',@con_surname_stg,' ',addrestg.[Last_Name_B])
					FROM 
						BT_ADDRESS_MASTER_CRM_stg addrestg		
					WHERE 
						addrestg.QMS_REF = @pQMSRefNo
						
					UPDATE 
						extra
					SET 
						extra.EX_altContactAddress  = @billingAddrID
					FROM 
						[dbo].CDMS_ACT_AE_EXTRA extra
					WHERE 
						extra.EX_AEID = @ENQAEID
				END
				ELSE
				BEGIN
					SET @MsgTxt = @MsgTxt + 'Corr addr upd2, '
					IF (@billingPostCode_stg is NOT NULL)--If billing address present
					BEGIN
						SET @MsgTxt = @MsgTxt + 'Corr addr upd2.1, '
						SELECT @billingAddrID = MAX(ADDRESS_ID)+1 FROM BT_ADDRESS_MASTER;
						SET @MsgTxt = @MsgTxt + 'Billing address id  '+ CONVERT(VARCHAR, @billingAddrID) + ', '
						--Insert billing address    
						INSERT INTO 
							BT_ADDRESS_MASTER  
							([ADDRESS_ID],[parentID], [addressLine1],[addressLine2], [addressLine3]  
							,[addressLine4],[addressLine5] ,[addressLine6],[addressLine7] 
							,[addressLine8],[addressLine9]  ,[postCode],[easting] ,[Northing] ,
							[addressUsage],
							[dateCreated]  ,[createdBy],[lastUpdated],[lastUpdatedBy] ,[CFSID],[deleted],[SOURCE_SYS],[addr_contactTel1],[addr_contactName])
						SELECT   
							@billingAddrID,@ENQAEID,addrestg.[Company_Name_B],addrestg.[addressLine2_B],addrestg.[addressLine3_B]  
							,addrestg.[addressLine4_B],addrestg.[addressLine5_B] ,addrestg.[addressLine6_B],addrestg.[addressLine7_B] 
							,addrestg.[addressLine8_B],addrestg.[addressLine9_B] ,addrestg.[postCode_B],addrestg.[easting_B] ,addrestg.[Northing_B] ,
							1, 
							GETDATE()  ,@ploggedonUser,GETDATE(),@ploggedonUser ,addrestg.[CFSID]_B,'N','CRM',addrestg.[Email_B],
							CONCAT(addrestg.[Title_B],' ',@con_forename_stg,' ',@con_surname_stg,' ',addrestg.[Last_Name_B])
						FROM 
							BT_ADDRESS_MASTER_CRM_stg addrestg		
						WHERE 
							addrestg.QMS_REF = @pQMSRefNo
						
						UPDATE 
							extra
						SET 
							extra.EX_altContactAddress  = @billingAddrID
						FROM 
							[dbo].CDMS_ACT_AE_EXTRA extra
						WHERE 
							extra.EX_AEID = @ENQAEID
					END
					ELSE--When billing address not present
					BEGIN
						SET @MsgTxt = @MsgTxt + 'Corr addr upd2.2, '
						SET @vAddIdO = @vAddIdA
						--Update address id in CDMS_ACT_AE_EXTRA for agent
						UPDATE 
							extra
						SET 
							extra.EX_altContactAddress  = @vAddIdO
						FROM 
							[dbo].CDMS_ACT_AE_EXTRA extra
						WHERE 
							extra.EX_AEID = @ENQAEID

							/*For Owner/occupier addresss details*/
							DECLARE @vAddIDForOwner BIGINT;

							SELECT @vAddIDForOwner = MAX(ADDRESS_ID)+1 FROM BT_ADDRESS_MASTER; 
							SET @MsgTxt = @MsgTxt + 'Corr addr upd2.3 '+CONVERT(VARCHAR,@vAddIDForOwner)+', '
							INSERT INTO 
								BT_ADDRESS_MASTER  
									([ADDRESS_ID],[parentID], [addressLine1],[addressLine2], [addressLine3]  
									,[addressLine4],[addressLine5] ,[addressLine6],[addressLine7] 
									,[addressLine8],[addressLine9]  ,[postCode],[easting] ,[Northing] ,
									[addressUsage],
									[dateCreated]  ,[createdBy],[lastUpdated],[lastUpdatedBy] ,[CFSID],[deleted],[SOURCE_SYS],[addr_contactTel1],[addr_contactName])
							SELECT   
								@vAddIDForOwner,@ENQAEID,@company,addrestg.[addressLine2],addrestg.[addressLine3]
								,addrestg.[addressLine4],addrestg.[addressLine5] ,addrestg.[addressLine6],addrestg.[addressLine7] 
								,addrestg.[addressLine8],addrestg.[addressLine9] ,addrestg.[postCode],addrestg.[easting] ,addrestg.[Northing] ,
								13, 
								GETDATE()  ,@ploggedonUser,GETDATE(),@ploggedonUser ,addrestg.[CFSID],'N','CRM',constg.con_telephone,
								CONCAT(constg.con_personalTitle,' ',constg.con_forename,' ',constg.con_surname)
							FROM   
								BT_ADDRESS_MASTER_CRM_STG addrestg
							INNER JOIN 
								BT_CONTACT_MASTER_CRM_STG constg
							ON 
								addrestg.QMS_REF=constg.QMS_REF
							WHERE 
								addrestg.QMS_REF = @pQMSRefNo

							UPDATE 
								BT_ENQUIRY_MASTER 
							SET 
								AE_correspondenceAddress=@vAddIDForOwner 
							WHERE 
								AE_AEID=@ENQAEID

					END
				END
				print 'End of Address insert'
				--Generate new quote ID for insert
				SELECT @vQuoteId =  MAX(AE_AEID)+1 FROM [BT_QUOTE_MASTER];
				DECLARE @OfferType Varchar(25)
				SELECT @OfferType=INT_LOV_ID FROM MT_SYS_LOV where LOV_NAME='SYSoffertype' and LOV_DESC='Standard';
				--Add QUOTE
				INSERT INTO 
					[dbo].[BT_QUOTE_MASTER]
					([AE_AEID],[AE_ParentID],[AE_RootID],AE_actualStartDate,AE_plannedEndDate,[AE_internalRef]
					,[AE_type],[AE_category],[AE_status] ,AE_noPhases,[AE_customer],[AE_summaryDescription]
					,[AE_dateCreated],[AE_createdBy],[AE_lastUpdated],[AE_lastUpdatedBy],[AE_deleted],[AE_designer]
					,AE_sendToCFS,AE_isTemplate,AE_MPANRequested,AE_address,AE_orderValue
					,AE_companyID,AE_metaModelType,AE_metaModelType2,AE_labourCost,AE_materialsCost,AE_contractCost,AE_totalCost,AE_labourRevenue,AE_materialsRevenue,AE_contractRevenue,
					AE_totalRevenue,AE_prelimCost,AE_capitalCost,AE_contestCost,AE_nonContestCost,
					AE_materialsMargin,AE_materialsVAT,AE_contractMargin,AE_contractVAT,AE_labourMargin,AE_labourVAT,
					AE_location,AE_LabourCostActual,AE_MaterialCostActual,AE_taskInstruction,AE_useUplift,AE_supplyCapacity,AE_generationType
					,AE_additionalGenCapacity,AE_supplyVoltage,AE_earthingMethod,AE_plot,AE_OfferType)
				SELECT
					@vQuoteId ,@VEnqId ,@VEnqId,GETDATE() ,QUOSTG.AE_plannedEndDate,
					'QUO'+ CONVERT(varchar(50), @vQuoteId)
					--,60
					,1
					, CASE 
					WHEN UPPER(LTRIM(RTRIM(QUOSTG.AE_category)))='NEW CONNECTION' THEN '8000' 
					WHEN UPPER(LTRIM(RTRIM(QUOSTG.AE_category)))='MODIFIED CONNECTION' THEN '0'--MN:Asked to james as it is not present into QPID
					WHEN UPPER(LTRIM(RTRIM(QUOSTG.AE_category)))='TEMPORARY CONNECTION' THEN '8500'
					WHEN UPPER(LTRIM(RTRIM(QUOSTG.AE_category)))='ADDITIONAL LOAD' THEN '0600' ELSE '0' END,
					--'7100'
					CASE WHEN UPPER(LTRIM(RTRIM(@quoteStatus))) = UPPER(LTRIM(RTRIM('Accepted')))
						THEN '7100'
						WHEN UPPER(LTRIM(RTRIM(@quoteStatus))) = UPPER(LTRIM(RTRIM('Pre Accepted')))
						THEN '7300'
						WHEN UPPER(LTRIM(RTRIM(@quoteStatus))) = UPPER(LTRIM(RTRIM('Live')))
						THEN '500'	ELSE '0' END
					,CASE WHEN UPPER(LTRIM(RTRIM(enqstg.AE_serviceType)))=UPPER(LTRIM(RTRIM('Single Phase')))
					THEN 1 
					WHEN UPPER(LTRIM(RTRIM(enqstg.AE_serviceType)))=UPPER(LTRIM(RTRIM('Two Phase')))
					THEN 2
					WHEN UPPER(LTRIM(RTRIM(enqstg.AE_serviceType)))=UPPER(LTRIM(RTRIM('Three Phase')))
					THEN 2
					WHEN UPPER(LTRIM(RTRIM(enqstg.AE_serviceType)))=UPPER(LTRIM(RTRIM('Split Phase')))
					THEN 2
					ELSE 0 END
					,@ccustid,REPLACE(REPLACE(REPLACE(enqstg.AE_summaryDescription, CHAR(13),' '), CHAR(10),' '),'&',' ')
					,CAST(enqstg.AE_dateOrigin AS SMALLDATETIME),@pLoggedOnUser,GETDATE(),@pLoggedOnUser,'N'
					,@QuoteDesignerId--AE_designer-- Add a new resource for QMS User and use it here
					,'N','N','N',@ae_address,0,1000,'T',6,0,0,0,0,0,0,0,0,0,0,0,0,
					0,20,0,20,0,20,
					CASE WHEN UPPER(LTRIM(RTRIM(QUOSTG.AE_location)))=UPPER(LTRIM(RTRIM('Single Premises')))THEN 1 
					WHEN UPPER(LTRIM(RTRIM(QUOSTG.AE_location)))=UPPER(LTRIM(RTRIM('Multiple Premises')))THEN 2 
					ELSE 0 END
					,0,0,'Y','Y',isnull(QUOSTG.AE_supplyCapacity,0.00)
					,(SELECT LOV_VALUE FROM MT_SYS_LOV WHERE rtrim(ltrim(coalesce(LOV_Name,'')))='sysGenerationType' AND LOV_DELETED=0  AND ISNULL(LOV_OBSOLETE,'N') <> 'Y' AND  UPPER(LTRIM(RTRIM(LOV_DESC)))= UPPER(LTRIM(RTRIM(QUOSTG.AE_generationType))))--ISNULL((SELECT TOP 1 GenerationTypeVal_QPID FROM generationType_QPID_QMS_Map WHERE UPPER(LTRIM(RTRIM(GenerationTypeDesc_QMS)))=UPPER(LTRIM(RTRIM(QUOSTG.AE_generationType)))),'0')
					,QUOSTG.AE_additionalGenCapacity, 
					(SELECT LOV_VALUE FROM MT_SYS_LOV WHERE rtrim(ltrim(coalesce(LOV_Name,'')))='sysSupplyVoltage' AND LTRIM(RTRIM(UPPER(LOV_DESC)))=LTRIM(RTRIM(UPPER(QUOSTG.AE_supplyVoltage))) AND LOV_DELETED=0), 
					(SELECT LOV_VALUE FROM MT_SYS_LOV WHERE rtrim(ltrim(coalesce(LOV_Name,'')))='sysEarthMethod' AND LTRIM(RTRIM(UPPER(LOV_DESC)))=LTRIM(RTRIM(UPPER(QUOSTG.AE_earthingMethod))) AND LOV_DELETED=0),
					enqstg.AE_plotUnit,@OfferType
				FROM 
					[BT_QUOTE_MASTER_CRM_STG] QUOSTG
				INNER JOIN 
					/*BT_ENQUIRY_MASTER enq
				ON 
					enq.AE_externalRef2 = QUOSTG.QMS_REF
				INNER JOIN*/ 
					BT_ENQUIRY_MASTER_CRM_stg enqstg
				ON 
					enqstg.QMS_REF = QUOSTG.QMS_REF				
				WHERE 
					enqstg.QMS_REF = @pQMSRefNo
			
				SELECT @EX_actualSecondComer=EX_actualSecondComer FROM [BT_QUOTE_MASTER_STG] WHERE QMS_REF=@pQMSRefNo


				--- ANIRBAN DHARA
				SELECT @connectionnature =  ConnectionNature, @connectiontypechange = ConnectionTypeChange FROM [dbo].[BT_QUOTE_MASTER_CRM_STG] where QMS_REF=@pQMSRefNo


				--SELECT @vEX_ID =  MAX([EX_ID])+1 FROM [BT_QUOTE_AE_EXTRA];
				--Insert into BT_QUOTE_AE_EXTRA
				INSERT INTO 
					[dbo].[BT_QUOTE_AE_EXTRA]
					([EX_AEID] ,[EX_actualSecondComer] ,[EX_clawBackAmount],[EX_paymentIssued]
					,[EX_origSchemeRef] ,[EX_dateCreated] ,[EX_dateCreatedBy],[EX_lastUpdated]
					,[EX_lastUpdatedBy] ,[EX_deleted],[EX_totalExportCapacity]
					,[EX_PO_RefNumber] ,[EX_InnovativeSolution] ,[EX_AdoptedAssetValue],[ConnectionNature],[ConnectionTypeChange])			
				SELECT 
					@vQuoteId,
					CASE 
					WHEN UPPER(LTRIM(RTRIM(@EX_actualSecondComer)))='YES' THEN 'Yes' 
					WHEN UPPER(LTRIM(RTRIM(@EX_actualSecondComer)))='NO' Then 'No' ELSE '0' END
					,0,null
					,null,GETDATE(),@pLoggedOnUser,GETDATE(),
					@pLoggedOnUser,'N',null,null,null,null,
					(SELECT INT_LOV_ID FROM MT_SYS_LOV WHERE LOV_NAME='sysConnectionNature' AND REPLACE(LOV_DESC,' ','') = REPLACE(@connectionnature,' ','')),
					(SELECT INT_LOV_ID FROM MT_SYS_LOV WHERE LOV_NAME='sysConnectionTypeChange' AND REPLACE(LOV_DESC,' ','') = REPLACE(@connectiontypechange,' ',''))
			

				--SELECT  'QUO'+ CONVERT(varchar(50), @vQuoteId)
			

				SELECT 
					@pCategory_addHist=AE_category,@ptype_addHist=AE_type,@pStatus_addHist=AE_status 
				FROM 
					BT_QUOTE_MASTER 
				WHERE 
					AE_AEID=@vQuoteId;

				-- To be discuss add the staging table type,category,status
				EXEC sp_ACT_addHIST_addQuote @vQuoteId, @ploggedonUser, @pCategory_addHist, @ptype_addHist, '', '', 0, @pStatus_addHist
				print 'End of Quote insert'
				
				-- Insert Estimate
								
				SELECT 
					@networkdist=AE_networkDistributor 
				FROM 
					BT_ENQUIRY_MASTER 
				WHERE 
					AE_AEID = @ENQAEID/*IN (SELECT AE_RootID FROM BT_QUOTE_MASTER --##Changed
					WHERE AE_internalRef='QUO'+ CONVERT(varchar(50), @vQuoteId))*/--TO DO
			
				IF @networkdist=4000
				BEGIN
					SET @iAE_contractZone='zones1and2';
				END
				ELSE
				BEGIN
					SET @iAE_contractZone='zones3and4';
				END
				
				--Generate new ID for estimate insert
				SELECT @newAEID=MAX(AE_AEID)+1 FROM BT_ESTIMATE_MASTER
			
				--Insert estimate
				INSERT INTO 
					BT_ESTIMATE_MASTER(AE_AEID,AE_ParentID,AE_RootID,AE_customer,
					AE_type,AE_category,AE_status,AE_contractZone,AE_address,AE_metaModelType,AE_metaModelType2,
					AE_dateCreated,AE_createdBy,AE_lastUpdated,AE_lastUpdatedBy,AE_deleted,AE_sendToCFS,
					AE_isTemplate,AE_MPANRequested,AE_internalRef,AE_useUplift
					,AE_taskInstruction,AE_labourCost,AE_materialsCost,AE_contractCost,AE_companyID
					)
				VALUES
					(@newAEID,@vquoteID,@vEnqID,@ccustid,-1,'-1',1,@iAE_contractZone,/*@ae_address*/@vAddIdS,'T',17,getDate(),
					@ploggedonUser,getDate(),@ploggedonUser,'N','N','N','N',CONCAT('EST',@newAEID)
					,'Y','Y',0,0,0,1000);

			print 'End of Estimate insert'
		
			/*Insert Standard*/
IF EXISTS(SELECT ID FROM BT_ENQUIRY_MASTER_CRM_STG where AE_summaryDescription like'%Connect Direct - Auto Approved%' and QMS_REF=@pQMSRefNo )BEGIN
 UPDATE BT_ENQUIRY_MASTER SET AE_assignedToPerson=41940, AE_quoteDesigner=41940 where AE_AEID=@VEnqId
			DECLARE @pSLAStartDate VARCHAR(50)
				SET @pSLAStartDate=(SELECT FORMAT(GETDATE(), 'yyyy-MM-dd HH:mm'))
			SELECT @vMaxStdId =  MAX(STA_ID)+1 FROM [BT_ACT_STANDARDS];
           SET @MsgTxt = @MsgTxt + 'Standard id '+convert(varchar,@vMaxStdId)+', ';
       INSERT INTO 
				[dbo].[BT_ACT_STANDARDS]
				([STA_ID],[STA_AEID],[STA_STD_ID],[sta_sla_date],[sta_target_date]
				,[sta_achieved_date],[sta_isDeleted],[sta_isDeletedReason],[sta_isExempt]
				,[sta_exemptReason],[sta_isStopped],[sta_created_by],[sta_created_date],[sta_updated_by],[sta_updated_date]
				--sta_created_from
				)
			SELECT
			   @vMaxStdId,@VEnqId,294,Stdstg.sta_SLA_date_2,Stdstg.sta_Target_date_2
			   ,Stdstg.sta_Achieved_date_2 ,'N',NULL,Stdstg.sta_isExempt_2
			   ,NULL,Stdstg.sta_Isstopped_2,@ploggedonUser,GETDATE(),@ploggedonUser,GETDATE()
			FROM
				BT_ACT_STANDARDS_CRM_STG AS Stdstg
			WHERE
				Stdstg.QMS_REF = @pQMSRefNo;
				DECLARE @ID BIGINT
				 SET @ID=(SELECT STA_ID FROM BT_ACT_STANDARDS where STA_AEID=@VEnqId and STA_STD_ID=294)
------------------------------------------Achive SLC12----------------------------------------------------------------------------------------------------------
                 EXEC sp_updateActiveStandardTargetDate @ID,@pSLAStartDate,@vAE_internalRef,@ploggedonUser
			
-------------------------------------------Apply IS LCT NOTIFICATION---------------------------------------------------------------------------------------------------
				SELECT @vMaxStdId =  MAX(STA_ID)+1 FROM [BT_ACT_STANDARDS];
				SET @MsgTxt = @MsgTxt + 'Standard id '+convert(varchar,@vMaxStdId)+', ';
				 EXEC SP_ADD_STANDARDS @ploggedonUser,@vAE_internalRef,'351',@pSLAStartDate,'',''
				 
				 SET @ID=(SELECT STA_ID FROM BT_ACT_STANDARDS where STA_AEID=@VEnqId and STA_STD_ID=351)
				
------------------------------------------Achive IS LCT Notification------------------------------------------------------------------------------------------
                 EXEC sp_updateActiveStandardTargetDate @ID,@pSLAStartDate,@vAE_internalRef,@ploggedonUser
			
			END
			ELSE BEGIN
				SELECT @vMaxStdId =  MAX(STA_ID)+1 FROM [BT_ACT_STANDARDS];


				SET @MsgTxt = @MsgTxt + 'Standard id '+convert(varchar,@vMaxStdId)+', ';

				INSERT INTO 
					[dbo].[BT_ACT_STANDARDS]
					([STA_ID],[STA_AEID],[STA_STD_ID],[sta_sla_date],[sta_target_date]
					,[sta_achieved_date],[sta_isDeleted],[sta_isDeletedReason],[sta_isExempt]
					,[sta_exemptReason],[sta_isStopped],[sta_created_by],[sta_created_date],[sta_updated_by],[sta_updated_date]
					--sta_created_from
					)
				SELECT
				   @vMaxStdId,@VEnqId,294,Stdstg.sta_SLA_date_2,Stdstg.sta_Target_date_2
				   ,Stdstg.sta_Achieved_date_2 ,'N',NULL,Stdstg.sta_isExempt_2
				   ,NULL,Stdstg.sta_Isstopped_2,@ploggedonUser,GETDATE(),@ploggedonUser,GETDATE()
				FROM
					BT_ACT_STANDARDS_CRM_STG AS Stdstg
				WHERE
					Stdstg.QMS_REF = @pQMSRefNo;
					END
				/* End  of Insert Standard---------------------------------------------------------------------------------------*/

				/* Start of Insert---------------------------------------------------------------------------------------*/

				UPDATE BT_Message SET Message_Status = 'P', Customer_Name=@vOwnerOnly_OrgName, Error_Description='' WHERE QMS_Reference_Number = @pQMSRefNo
				 
			COMMIT TRAN

			--Set execution end time
			set @ExecEndTime=GETDATE();
			--set @MsgTxt = 'Excution time '+CONVERT( VARCHAR(24), @ExecStartTime, 121)+'-'+CONVERT( VARCHAR(24), @ExecEndTime, 121);
			SET @MsgTxt = @MsgTxt + 'END '+ CONVERT( VARCHAR(24), @ExecEndTime, 121);
			exec usp_CreateEntryForExecutionTime @pQMSRefNo,@MsgTxt;
			--Increment iteration variable
			set @i = @i + 1;
			--Increment page number
			set @vpageNumber = @vpageNumber + 1
		END

		exec SP_LCT_MapEquipmentDataToTechType @vAE_internalRef

		-- Drop temp table
		drop table #BTMsgTmp;

		--BF01: release the run lock
		EXEC sp_releaseapplock @Resource = 'QPID_CRM_FinalDataLoad', @LockOwner = 'Session';

	END TRY

	BEGIN CATCH
		--BF01: no transaction is open when the error comes before BEGIN TRAN
		--(a bare ROLLBACK raised error 3903 there)
		IF @@TRANCOUNT > 0
			ROLLBACK TRAN
		IF APPLOCK_MODE('public', 'QPID_CRM_FinalDataLoad', 'Session') <> 'NoLock'
			EXEC sp_releaseapplock @Resource = 'QPID_CRM_FinalDataLoad', @LockOwner = 'Session';
		SELECT @pErrorMessage='Error Code: ' + CAST (ERROR_NUMBER() AS VARCHAR(10)) + 'Line Number: ' + CAST(ERROR_LINE() as nvarchar(20))+', Error: ' + ERROR_MESSAGE()
		print @pErrorMessage	
		UPDATE BT_Message SET Error_Description=LEFT(@pErrorMessage, 500),Message_Status='F' WHERE QMS_Reference_Number = @pQMSRefNo AND Message_Status = 'I'
		--BF01: Error_Description is varchar(500); a message already committed as 'P' is never overwritten
		--For logging
		SET @MsgTxt = @MsgTxt + 'END WITH ERROR '+ CONVERT( VARCHAR(24), GETDATE(), 121);
		exec usp_CreateEntryForExecutionTime @pQMSRefNo,@MsgTxt;
	END CATCH
END
GO
