/******************************************************************************
  BUG FIX BF01 - PENDING VALIDATION - DO NOT DEPLOY UNTESTED
  Procedure : [dbo].[usp_QMS_FinalDataLoad_CRM_SmallWork]
  Base      : QPID_SP/usp_QMS_FinalDataLoad_CRM_SmallWork.sql (production original)
  Fix       : BF01 (2026-10-21)
  Generated : bugfix/tools/gen_bugfix.py (inverse transform = base, checked)
  Deploy    : together with the other CRM loader (usp_QMS_FinalDataLoad_CRM_MajorWork);
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
  ROLLBACK  : bugfix/rollback/usp_QMS_FinalDataLoad_CRM_SmallWork_ORIGINAL.sql
******************************************************************************/
SET ANSI_NULLS ON
GO
SET QUOTED_IDENTIFIER ON
GO
ALTER PROCEDURE [dbo].[usp_QMS_FinalDataLoad_CRM_SmallWork]-- ''
	
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
	DECLARE @vAPrefix VARCHAR(50);
	DECLARE @vASurname VARCHAR(50);
	DECLARE @vAForeName VARCHAR(50);
	DECLARE @vTele VARCHAR(50);
	DECLARE @vConcatinatedOrganisationName VARCHAR(50)='';
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

	DECLARE @con_forename_stg VARCHAR(50);
	DECLARE @con_surname_stg VARCHAR(50);
	DECLARE @con_personalTitle_stg VARCHAR(50);
	DECLARE @con_telephone_stg VARCHAR(50);
	DECLARE @con_mobile_stg VARCHAR(50);
	DECLARE @con_email_stg VARCHAR(50);	
	DECLARE @ccustPremConID INT;
	DECLARE @custName VARCHAR(50)='';
	DECLARE @MsgTxt varchar(2000);
	DECLARE @ae_address int;
	DECLARE @addressPostcode_stg VARCHAR(50);
	DECLARE @addressEasting_stg VARCHAR(10);
	DECLARE @addressNorthing_stg VARCHAR(10);
	DECLARE @addressDesc_stg VARCHAR(255);
	DECLARE @addressSitename_stg VARCHAR(250);
	DECLARE @addressCFSID_stg INT;
	DECLARE @contactID int;
	DECLARE @vCustomerAddressId int;
	DECLARE @vAddIdS int;
	DECLARE @vOwnerOccupiedAddrId int;
	DECLARE @vAddIdO int;
	DECLARE @VEnqId int;
	DECLARE @vQuoteId int;
	DECLARE @vCustId int;
	DECLARE @QuoteDesigner VARCHAR(50);
	DECLARE @QuoteDesignerId INT;
	DECLARE @EX_actualSecondComer VARCHAR(50)=NULL;
	DECLARE @pCategory_addHist VARCHAR(50)=NULL;
	DECLARE @ptype_addHist INT=0;
	DECLARE @pStatus_addHist VARCHAR(50)=NULL;
	DECLARE @newAEID int;
	DECLARE @networkdist int;
	DECLARE @iAE_contractZone varchar(100);
	DECLARE @viBOM_priceBook VARCHAR(50);
	DECLARE @vBOM_ID INT;
	DECLARE @vBOMCount int;
	DECLARE @vBOMMinCount int;
	DECLARE @vBOMMaxCount int;
	DECLARE @vCustContPercentage float;
	DECLARE @vprice_book_type_qms VARCHAR(50);
	DECLARE @vDistributor_Type_QMS VARCHAR(50);
	DECLARE @vBOmType VARCHAR(50);
	DECLARE @viBOM_estimateParentRef VARCHAR(50);
	DECLARE @viBOM_priceBooktype VARCHAR(50);
	DECLARE @vBOMDesc VARCHAR(5000);
	DECLARE @vnewBOMID VARCHAR(50);
	DECLARE @vQty VARCHAR(50);
	DECLARE @vItemTaskCat VARCHAR(500);
	DECLARE @vTCI_Type VARCHAR(50);
	DECLARE @vItemMaxCount int;
	DECLARE @vItemMinCount int;
	DECLARE @vItem_ID int;
	DECLARE @vIGS_stdCode_4 VARCHAR(50);
	DECLARE @vInvestmentDriver VARCHAR(10);
	DECLARE @vItemTypeDesc_QPID VARCHAR(500);
	DECLARE @vTempNewBOMID VARCHAR(50)=NULL;	
	DECLARE @vTempInvestmentDriver VARCHAR(50);
	DECLARE @vEstimatedParentID INT;
	DECLARE @vProjOrg VARCHAR(50);
	DECLARE @vTemplateType VARCHAR(50);
	DECLARE @ae_assignedToPerson VARCHAR(50);
	DECLARE @assignToPersonID INT;
	DECLARE @defaultPersonID INT;
	DECLARE @defaultDeptID INT;
	DECLARE @defaultBomTaskType VARCHAR(50);
	DECLARE @vMaxStdId int;
	DECLARE @correspondenceAddrId BIGINT;
	DECLARE @quoteStatus VARCHAR(50);
	DECLARE @bom_InvestmentDriver VARCHAR(100);
	DECLARE @quoteStatusID VARCHAR(50);
	DECLARE @btMsgID INT;
	DECLARE @vBOM_generatedProject INT;
	DECLARE @vBoMUpdMinCount INT;
	DECLARE @vBoMUpdMaxCount INT;
	DECLARE @agentCompany VARCHAR(250);
	DECLARE @company VARCHAR(250);
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
	
		set @vpageSize = 1;
		set @vpageNumber = 0;
		set @i = 0;

		-- Create CTE for BT_MESSAGE
		/*WITH BTMESSAGECte(QMSRef,QPIDRef,Status) as
		(select QMS_REFERENCE_NUMBER, QPID_REFERENCE_NUMBER, MESSAGE_STATUS 
		from BT_MESSAGE where Message_Status = 'W' and [Source] = 'CRM_SmallWork')

		-- Set record count of CTE
		select @vcntBtMsg=count(*) from BTMESSAGECte;
		*/
		--Get Bt_Message data in temp table
		select top 1 * into #BTMsgTmp from BT_MESSAGE where Message_Status = 'W' and [Source] = 'CRM_SmallWork';

		-- Set record count of temp table
		select @vcntBtMsg=count(*) from #BTMsgTmp;

		WHILE @i < @vcntBtMsg
		BEGIN
		
			SET @MsgTxt = 'START '+CONVERT( VARCHAR(24), GETDATE(), 121)+', '
			--Reseting values
			
			SET @assignToPersonID = null
			SET @vDepartment_Qpid_value = null
			SET @vInvestmentDriver = null
			SET @viBOM_priceBooktype = null
			SET @bom_InvestmentDriver = null
			--Fetch record from Temp table one by one
			SELECT 
				@pQMSRefNo = QMS_REFERENCE_NUMBER,
				@quoteStatus = QUOTE_STATUS,
				@btMsgID = ID
			FROM 
				#BTMsgTmp 
			ORDER BY 
				QMS_REFERENCE_NUMBER 
			OFFSET 
				@vpageNumber 
			ROWS FETCH NEXT 
				@vpageSize 
			ROWS ONLY
			print @pQMSRefNo

			--Set in progress status in BT_Message
			UPDATE
				BT_Message
			SET
				Message_Status = 'I'
			WHERE
				--QMS_REFERENCE_NUMBER = @pQMSRefNo;
				ID=@btMsgID

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
				
			--Set enquiry id, enq ref for selected bt message row
			SELECT 
				@ENQAEID= AE_AEID , 
				@vAE_internalRef = AE_internalRef,
				@vCUST = AE_customer,
				@ae_address=AE_customeraddress,
				@contactID = AE_Contact
			FROM 
				BT_ENQUIRY_MASTER 
			WITH (NOLOCK)
			WHERE 
				AE_internalRef=(SELECT TOP 1 QPID_Reference_Number 
						FROM #BTMsgTmp WHERE QMS_Reference_Number=@pQMSRefNo 
						ORDER BY ID DESC) 
			SET @MsgTxt = @MsgTxt + 'AE enq,addr,cont ' + CONVERT(VARCHAR,@ENQAEID)+','+ CONVERT(VARCHAR,@ae_address)+','+ CONVERT(VARCHAR,@contactID)+', '

			SET @correspondenceAddrId = @ae_address 
			SET @VEnqId = @ENQAEID
			--Select owner org name, telephone, cust type for selected bt message row
			SELECT 
				@vOwnerOnly_OrgName = ISNULL(LTRIM(RTRIM(((CUST_ORG_NAME)))),''), 
				@vOwnerOnly_Telephone = ISNULL(LTRIM(RTRIM(((CUST_TELEPHONE)))),''), 
				@vOwnerOnly_Custtype = ISNULL(LTRIM(RTRIM(((CUST_TYPE)))),'')  
			FROM 
				BT_CUSTOMER_MASTER_CRM_STG 
			WITH (NOLOCK)
			WHERE 
				QMS_REF=@pQMSRefNo

			--Select owner occupied or agent
			SELECT 
				@vOwnerOrAgentOccupier = AE_ExternalRef3 
			FROM 
				BT_ENQUIRY_MASTER_CRM_STG 
			WITH (NOLOCK)
			WHERE 
				QMS_REF=@pQMSRefNo
			
			--Select contact details from contact master stg for selected bt message row
			SELECT 
				top 1 @vdomesticorCommercial = domesticorCommercial,
				@vdomesticorCommercial_A = ISNULL(LTRIM(RTRIM(domesticorCommercial_A)),''),
				@vAForeName = ISNULL(LTRIM(RTRIM(con_forename_A)),''),
				@vASurname = ISNULL(LTRIM(RTRIM(con_surname_A)),''),
				@vAPrefix = ISNULL(LTRIM(RTRIM(con_personalTitle_A)),''),
				@vTele = ISNULL(LTRIM(RTRIM(con_telephone_A)),''),
				@agentCompany = ISNULL(LTRIM(RTRIM(((Agent_Company)))),''),
				@company = ISNULL(LTRIM(RTRIM(((Company)))),'')
			FROM 
				BT_CONTACT_MASTER_CRM_STG 
			WITH (NOLOCK)
			WHERE 
				QMS_REF=@pQMSRefNo
			
			SET @MsgTxt = @MsgTxt + 'AgntCompny:'+@agentCompany+' Company:'+@company+' , '
					
			
			
			-- Set organisation name		
			IF (@vdomesticorCommercial_A='DOMESTIC')
			BEGIN
				SET @vConcatinatedOrganisationName=CONCAT(@vAPrefix,' ',@vAForeName,' ',@vASurname)
			END
			ELSE
			BEGIN
				SET @vConcatinatedOrganisationName=CONCAT(@vAForeName,' ',@vASurname)
			END
			
			print @vdomesticorCommercial_A
			print @vConcatinatedOrganisationName
			
			--Set customer Name based on Agent or Owner
			IF @vOwnerOrAgentOccupier = 'Agent'
			BEGIN
				IF LEN(@agentCompany)>0
				BEGIN
					set @vConcatinatedOrganisationName = @agentCompany;
				END
			END
			IF @vOwnerOrAgentOccupier = 'Owner'
			BEGIN
				IF LEN(@company)>0
				BEGIN
					set @vOwnerOnly_OrgName = @company;
				END
			END
			
			-- Find any customer is exists for selected org name, telephone, cust name
			SELECT 
				@cnt = COUNT(1) 
			FROM 
				BT_CUSTOMER_MASTER 
			WITH (NOLOCK)
			WHERE 
				ISNULL(LTRIM(RTRIM(CUST_ORG_NAME)),'')= @vOwnerOnly_OrgName
				AND ISNULL(LTRIM(RTRIM(CUST_TELEPHONE)),'')= @vOwnerOnly_Telephone
				AND ISNULL(LTRIM(RTRIM(CUST_TYPE)),'')= @vOwnerOnly_Custtype
			print 'Owner count '+convert(varchar,@cnt)	
			SET @MsgTxt = @MsgTxt + @vOwnerOrAgentOccupier + ', '
			IF (@vOwnerOrAgentOccupier='Agent')
			BEGIN
				--Find record count for selected org name and telephone
				SELECT 
					@vcntAgnt = COUNT(1) 
				FROM 
					BT_CUSTOMER_MASTER 
				WITH (NOLOCK)
				WHERE
					LTRIM(RTRIM(CUST_ORG_NAME))= @vConcatinatedOrganisationName 
					AND ISNULL(CUST_TELEPHONE,'')=@vTele
				print 'Record found for agent -' +convert(varchar,@vcntAgnt)
				
				--Find CUST_PRIM_ADDRID for existing customer
				SELECT	
					@ccustPremAddrID=CUST_PRIM_ADDRID,
					@ccustPremConID=CUST_PRIM_CONTACTID
				FROM 
					BT_CUSTOMER_MASTER 
				WITH (NOLOCK)
				WHERE 
					LTRIM(RTRIM(CUST_ORG_NAME))=LTRIM(RTRIM(@vConcatinatedOrganisationName)) 
					AND ISNULL(CUST_TELEPHONE,'')=@vTele
				SET @MsgTxt = @MsgTxt + 'Existing addrID '+ CONVERT(VARCHAR, @ccustPremAddrID) + ', '	
				SET @MsgTxt = @MsgTxt + 'Existing contID '+ CONVERT(VARCHAR, @ccustPremConID) + ', '
				
				IF (@ccustPremAddrID > 0)
				BEGIN
					-- select address for existing customer
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
						BT_ADDRESS_MASTER 
					WITH (NOLOCK)
					WHERE 
						ADDRESS_ID=@ccustPremAddrID	and deleted != 'Y' 
				END
					--Select address lines from stg
				SELECT 
					@addressLine1_stg=ISNULL(UPPER(rtrim(ltrim(addressLine1_A))),''),
					@addressLine2_stg=ISNULL(UPPER(rtrim(ltrim(addressLine2_A))),''),
					@addressLine3_stg=ISNULL(UPPER(rtrim(ltrim(addressLine3_A))),''),
					@addressLine4_stg=ISNULL(UPPER(rtrim(ltrim(addressLine4_A))),''),
					@addressLine5_stg=ISNULL(UPPER(rtrim(ltrim(addressLine5_A))),''),
					@addressLine6_stg=ISNULL(UPPER(rtrim(ltrim(addressLine6_A))),''),
					@addressLine7_stg=ISNULL(UPPER(rtrim(ltrim(addressLine7_A))),''),
					@addressLine8_stg=ISNULL(UPPER(rtrim(ltrim(addressLine8_A))),''),
					@addressLine9_stg=ISNULL(UPPER(rtrim(ltrim(addressLine9_A))),''),
					@addressPostcode_stg=ISNULL(UPPER(rtrim(ltrim(postCode_A))),''),
					@addressEasting_stg =ISNULL(UPPER(rtrim(ltrim(easting_A))),''),
					@addressNorthing_stg=ISNULL(UPPER(rtrim(ltrim(northing_A))),''),
					@addressDesc_stg=ISNULL(UPPER(rtrim(ltrim(description_A))),''),
					@addressSitename_stg=ISNULL(UPPER(rtrim(ltrim(siteName_A))),''),
					@addressCFSID_stg=CFSID_A
				FROM 
					BT_ADDRESS_MASTER_CRM_STG 
				WITH (NOLOCK)
				WHERE 
					QMS_REF=@pQMSRefNo
					
				--For contact
				
				IF(@ccustPremConID>0)
				BEGIN
					
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
				SELECT 
					@con_forename_stg=ISNULL(UPPER(rtrim(ltrim(con_forename_A))),''),
					@con_surname_stg=ISNULL(UPPER(rtrim(ltrim(con_surname_A))),''),
					@con_personalTitle_stg=ISNULL(UPPER(rtrim(ltrim(con_personalTitle_A))),''),
					@con_telephone_stg=ISNULL(UPPER(rtrim(ltrim(con_telephone_A))),''),
					@con_mobile_stg=ISNULL(UPPER(rtrim(ltrim(con_mobile_A))),''),
					@con_email_stg=ISNULL(UPPER(rtrim(ltrim(con_email_A))),'')
				FROM 
					BT_CONTACT_MASTER_CRM_STG 
				WITH (NOLOCK)
				WHERE 
					QMS_REF=@pQMSRefNo
				
					
			END
			ELSE IF (@cnt > 0) -- For owner
			BEGIN
				--Select address id for owner
				SELECT 
					@ccustPremAddrID=CUST_PRIM_ADDRID,
					@ccustPremConID=CUST_PRIM_CONTACTID
				FROM 
					BT_CUSTOMER_MASTER 
				WITH (NOLOCK)
				WHERE 
					ISNULL(rtrim(ltrim(CUST_ORG_NAME)),'')=@vOwnerOnly_OrgName
					AND ISNULL(rtrim(ltrim(CUST_TELEPHONE)),'')=@vOwnerOnly_Telephone
					AND ISNULL(rtrim(ltrim(CUST_TYPE)),'')=@vOwnerOnly_Custtype
					
				SET @MsgTxt = @MsgTxt + 'Existing addrID '+ CONVERT(VARCHAR, @ccustPremAddrID) + ', '	
				SET @MsgTxt = @MsgTxt + 'Existing contID '+ CONVERT(VARCHAR, @ccustPremConID) + ', '	
				
				IF(@ccustPremAddrID>0)
				BEGIN
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
				END

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
					@addressSitename_stg=ISNULL(UPPER(rtrim(ltrim(siteName))),''),
					@addressCFSID_stg=CFSID
				FROM 
					BT_ADDRESS_MASTER_CRM_STG 
				WITH (NOLOCK)
				WHERE 
					QMS_REF=@pQMSRefNo
					
				--For contact
				
				IF(@ccustPremConID>0)
				BEGIN
					
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
				SELECT 
					@con_forename_stg=ISNULL(UPPER(rtrim(ltrim(con_forename))),''),
					@con_surname_stg=ISNULL(UPPER(rtrim(ltrim(con_surname))),''),
					@con_personalTitle_stg=ISNULL(UPPER(rtrim(ltrim(con_personalTitle))),''),
					@con_telephone_stg=ISNULL(UPPER(rtrim(ltrim(con_telephone))),''),
					@con_mobile_stg=ISNULL(UPPER(rtrim(ltrim(con_mobile))),''),
					@con_email_stg=ISNULL(UPPER(rtrim(ltrim(con_email))),'')
				FROM 
					BT_CONTACT_MASTER_CRM_STG 
				WITH (NOLOCK)
				WHERE 
					QMS_REF=@pQMSRefNo
			END
			ELSE -- Agent/Owner record does not matched with existing customer
			BEGIN
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
					@addressSitename_stg=ISNULL(UPPER(rtrim(ltrim(siteName))),''),
					@addressCFSID_stg=CFSID
				FROM 
					BT_ADDRESS_MASTER_CRM_STG 
				WITH (NOLOCK)
				WHERE 
					QMS_REF=@pQMSRefNo
					
				SELECT 
					@con_forename_stg=ISNULL(UPPER(rtrim(ltrim(con_forename))),''),
					@con_surname_stg=ISNULL(UPPER(rtrim(ltrim(con_surname))),''),
					@con_personalTitle_stg=ISNULL(UPPER(rtrim(ltrim(con_personalTitle))),''),
					@con_telephone_stg=ISNULL(UPPER(rtrim(ltrim(con_telephone))),''),
					@con_mobile_stg=ISNULL(UPPER(rtrim(ltrim(con_mobile))),''),
					@con_email_stg=ISNULL(UPPER(rtrim(ltrim(con_email))),'')
				FROM 
					BT_CONTACT_MASTER_CRM_STG 
				WITH (NOLOCK)
				WHERE 
					QMS_REF=@pQMSRefNo
					
			END
			
			

			--Select Quote designer ID
			SELECT  
				@QuoteDesigner = QUOSTG.QuoreDesigner
			FROM 
				[BT_QUOTE_MASTER_CRM_STG] QUOSTG
			WITH (NOLOCK)	
			WHERE 
				QUOSTG.QMS_REF = @pQMSRefNo
		
			--For Default value
			SET @QuoteDesigner = ISNULL(UPPER(rtrim(ltrim(@QuoteDesigner))),'NOT ALLOCATED');
			
			SELECT 
				TOP 1 @QuoteDesignerId =  PEOPORG_PEOPORGID 
			FROM 
				MT_ACT_PEOPORG 
			WITH (NOLOCK)
			WHERE 
				PEOPORG_ASSIGNEEINDIV= 'Y' 
				AND LTRIM(RTRIM(UPPER(PEOPORG_NAME)))=RTRIM(LTRIM(UPPER(@QuoteDesigner))) 
				AND peopOrg_designer = 'Y' AND PEOPORG_DELETED <> 'Y'
			
			--Select second comer 
			SELECT 
				@EX_actualSecondComer=EX_actualSecondComer 
			FROM 
				[BT_QUOTE_MASTER_CRM_STG] 
			WITH (NOLOCK)
			WHERE 
				QMS_REF=@pQMSRefNo

			
			SET @MsgTxt = @MsgTxt + 'End select '+CONVERT( VARCHAR(24), GETDATE(), 121)+', '
			
			SET TRANSACTION ISOLATION LEVEL READ UNCOMMITTED
			BEGIN TRAN
			BEGIN
			
				
					
			-- Customer Master Update
				/*SELECT @vCUST = CUST_ID
						FROM [BT_CUSTOMER_MASTER] cm
						INNER JOIN BT_ENQUIRY_MASTER enq
						ON enq.AE_customer = cm.cust_id	
						INNER JOIN BT_CUSTOMER_MASTER_CRM_stg custg
						ON custg.QMS_REF = enq.AE_externalRef2
						WHERE enq.AE_externalRef2 = @pQMSRefNo*/
						
				-- Set organisation name		
				
				IF (@vOwnerOrAgentOccupier='Agent')
				BEGIN			
					SET @MsgTxt = @MsgTxt + 'Cust upd1, '
				
					IF (@vcntAgnt > 0) --If record exists, tag the cust id to new enquiry and estimate
					BEGIN
						SET @MsgTxt = @MsgTxt + 'Cust upd2, '
						--Find existing customer's cust id
						SELECT 
							@ccustid=ccm.CUST_ID 
						FROM 
							BT_CUSTOMER_MASTER ccm 
						WITH (NOLOCK)
						WHERE 
							CUST_ORG_NAME=@vConcatinatedOrganisationName 
							AND ISNULL(CUST_TELEPHONE,'')=@vTele

						--Tag the cust id to the enquiry
						UPDATE 
							BT_ENQUIRY_MASTER 
						SET 
							AE_customer=@ccustid 
						WHERE 
							AE_externalRef2=@pQMSRefNo

						--Tag the cust id to estimate
						UPDATE 
							BT_ESTIMATE_MASTER 
						SET 
							AE_customer=@ccustid 
						WHERE 
							AE_RootID=@ENQAEID
						print 'cust id '+convert(varchar,@ccustid)
					END
					ELSE -- If no existing customer found
					BEGIN
					print 'Record not found for agent '
						SET @MsgTxt = @MsgTxt + 'Cust upd3, '
						--Update information from stg to main table
						UPDATE  
							BT_CUSTOMER_MASTER 
						SET 
							[CUST_ORG_NAME] =@vConcatinatedOrganisationName --CONCAT(ISNULL(@vAPrefix,''),' ',ISNULL(@vAForeName,''),' ',ISNULL(@vASurname,''))
							,[CUST_TELEPHONE] = @vTele
							,[CUST_PRIM_CONTACTID] = @contactID
							,[CUST_PRIM_ADDRID] = @ae_address
							,[UPDATED_ON] = GETDATE()
							,[UPDATED_BY] = @ploggedonUser
							,[CUST_TYPE] = LTRIM(RTRIM(@vdomesticorCommercial_A))
						WHERE
							CUST_ID = @vCUST;
						/*FROM 
							[BT_CUSTOMER_MASTER] cm
							INNER JOIN BT_ENQUIRY_MASTER enq
										ON enq.AE_customer = cm.cust_id
										INNER JOIN BT_CUSTOMER_MASTER_CRM_stg custg
										ON custg.QMS_REF = enq.AE_externalRef2
										INNER JOIN BT_CONTACT_MASTER_CRM_STG cont
										ON cont.QMS_REF = enq.AE_externalRef2
										WHERE enq.AE_externalRef2 = @pQMSRefNo*/
						SET @ccustid = @vCUST;
					END
				
					-- set Org name to update in BT_Message
					SET @custName = @vConcatinatedOrganisationName 
					
				END
				ELSE IF (@cnt > 0) --If owner customer exists
				BEGIN
					SET @MsgTxt = @MsgTxt + 'Cust upd4, '
					--Find existing customer's cust id
					SELECT 
						@ccustid=CUST_ID
					FROM 
						BT_CUSTOMER_MASTER 
					WITH (NOLOCK)
					WHERE 
						ISNULL(LTRIM(RTRIM(CUST_ORG_NAME)),'')=@vOwnerOnly_OrgName 
						AND ISNULL(LTRIM(RTRIM(CUST_TELEPHONE)),'')=@vOwnerOnly_Telephone
						AND ISNULL(LTRIM(RTRIM(CUST_TYPE)),'')=@vOwnerOnly_Custtype
					print 'owner cust id '+convert(varchar,@ccustid)
					
					--Tag cust id to enquiry
					UPDATE 
						BT_ENQUIRY_MASTER 
					SET 
						AE_customer=@ccustid 
					WHERE 
						AE_externalRef2=@pQMSRefNo

					--Tag cust id to estimate
					UPDATE 
						BT_ESTIMATE_MASTER 
					SET 
						AE_customer=@ccustid 
					WHERE 
						AE_RootID=@ENQAEID
						
					--set customer name to Update in BT_Message
					SET @custName = @vOwnerOnly_OrgName
				END
				ELSE -- If the record is neither agent nor owner
				BEGIN
					SET @MsgTxt = @MsgTxt + 'Cust upd5, '
					UPDATE
						BT_CUSTOMER_MASTER
					SET 
						[CUST_ORG_NAME] = @vOwnerOnly_OrgName,
						[CUST_TELEPHONE] = @vOwnerOnly_Telephone
						,[CUST_PRIM_CONTACTID] = @contactID
						,[CUST_PRIM_ADDRID] = @AE_address
						,[UPDATED_ON] = GETDATE()
						,[UPDATED_BY] = @ploggedonUser
						,[CUST_TYPE] = LTRIM(RTRIM(@vOwnerOnly_Custtype))
					WHERE
						CUST_ID = @vCUST
								/*FROM [BT_CUSTOMER_MASTER] cm
								INNER JOIN BT_ENQUIRY_MASTER enq
								ON enq.AE_customer = cm.cust_id
								INNER JOIN BT_CUSTOMER_MASTER_CRM_stg custg
								ON custg.QMS_REF = enq.AE_externalRef2
								WHERE enq.AE_externalRef2 = @pQMSRefNo*/
								print 'for other update'
								
					--Set cust name to Update in BT_Message
					SET @custName=@vOwnerOnly_OrgName
					SET @ccustid = @vCUST;
				END
				SET @MsgTxt = @MsgTxt + 'End customer '+CONVERT( VARCHAR(24), GETDATE(), 121)+', '
				--Address update
				IF (@vOwnerOrAgentOccupier='Agent')
				BEGIN
					SET @MsgTxt = @MsgTxt + 'Addr upd1, '
					IF(@ccustPremAddrID>0)
					BEGIN
						SET @MsgTxt = @MsgTxt + 'Addr upd2, '
						--Compare address master with staging data
						IF((@addressLine1=@addressLine1_stg)and(@addressLine2=@addressLine2_stg)AND(@addressLine3=@addressLine3_stg)
						AND(@addressLine4=@addressLine4_stg)AND(@addressLine5=@addressLine5_stg)AND(@addressLine6=@addressLine6_stg)
						AND(@addressLine7=@addressLine7_stg) AND(@addressLine8=@addressLine8_stg)AND(@addressLine9=@addressLine9_stg))
						BEGIN
							SET @MsgTxt = @MsgTxt + 'Addr upd3, '
							--Select address id to mark deleted
							SELECT 
								@ccustPremAddrIDBKP=AE_address,
								@ccustPremConIDBKP=AE_contact 
							FROM 
								BT_ENQUIRY_MASTER 
							WITH (NOLOCK)
							WHERE 
								AE_externalRef2=@pQMSRefNo
							
							--Tag address id to enquiry and estimate
							UPDATE 
								BT_ENQUIRY_MASTER 
							SET 
								AE_address=@ccustPremAddrID
								,AE_correspondenceAddress=@ccustPremAddrID
								,AE_CustomerAddress=@ccustPremAddrID
							WHERE 
								AE_externalRef2=@pQMSRefNo
							
							--Mark address as deleted which created in initial load
							UPDATE 
								BT_ADDRESS_MASTER 
							SET 
								deleted='Y' 
							WHERE 
								ADDRESS_ID=@ccustPremAddrIDBKP
							
							--Update site name
							UPDATE 
								BT_ADDRESS_MASTER 
							SET 
								sitename =(dbo.fn_rpt_GetReformatReturnAddress_NoSiteName(CAST(@ccustPremAddrID AS INT)))
							WHERE 
								ADDRESS_ID=@ccustPremAddrID;

							UPDATE 
								BT_ESTIMATE_MASTER 
							SET 
								AE_address=@ccustPremAddrID 
							WHERE 
								AE_RootID=@ENQAEID
								
							SET @correspondenceAddrId = @ccustPremAddrID 
						END
						ELSE -- When address not matched
						BEGIN
							SET @MsgTxt = @MsgTxt + 'Addr upd4, '
							UPDATE  
								BT_ADDRESS_MASTER
							SET  
								[addressLine1] = @addressLine1_stg,[addressLine2] = @addressLine2_stg
								,[addressLine3] = @addressLine3_stg,[addressLine4] = @addressLine4_stg
								,[addressLine5] = @addressLine5_stg,[addressLine6] = @addressLine6_stg
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
									/*FROM [BT_CUSTOMER_MASTER] cm
									INNER JOIN BT_ENQUIRY_MASTER enq
									ON enq.AE_customer = cm.cust_id
									INNER JOIN BT_ADDRESS_MASTER addr
									ON addr.parentID = cm.CUST_ID
									INNER JOIN BT_ADDRESS_MASTER_CRM_stg addrestg
									ON addrestg.QMS_REF = enq.AE_externalRef2
									WHERE enq.AE_externalRef2 = @pQMSRefNo*/
						END
					END
					ELSE --When cust addr id <= 0
					BEGIN
						SET @MsgTxt = @MsgTxt + 'Addr upd5, '
						UPDATE  
							BT_ADDRESS_MASTER
						SET  
							[addressLine1] = @addressLine1_stg,[addressLine2] = @addressLine2_stg
							,[addressLine3] = @addressLine3_stg,[addressLine4] = @addressLine4_stg
							,[addressLine5] = @addressLine5_stg,[addressLine6] = @addressLine6_stg
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
							/*FROM [BT_CUSTOMER_MASTER] cm
							INNER JOIN BT_ENQUIRY_MASTER enq
							ON enq.AE_customer = cm.cust_id
							INNER JOIN BT_ADDRESS_MASTER addr
							ON addr.parentID = cm.CUST_ID
							INNER JOIN BT_ADDRESS_MASTER_CRM_stg addrestg
							ON addrestg.QMS_REF = enq.AE_externalRef2
							WHERE enq.AE_externalRef2 = @pQMSRefNo*/
					END
				END
				ELSE IF (@cnt > 0) --Customer found for owner org name, telephone and cust type 
				BEGIN
					SET @MsgTxt = @MsgTxt + 'Addr upd6, '
					print 'addr '+@addressLine3
					print 'addrstg '+@addressLine3_stg
					IF(@ccustPremAddrID>0)
					BEGIN
						SET @MsgTxt = @MsgTxt + 'Addr upd7, '
						IF((@addressLine1=@addressLine1_stg)and(@addressLine2=@addressLine2_stg)AND(@addressLine3=@addressLine3_stg)
						AND(@addressLine4=@addressLine4_stg)AND(@addressLine5=@addressLine5_stg)AND(@addressLine6=@addressLine6_stg)
						AND(@addressLine7=@addressLine7_stg) AND(@addressLine8=@addressLine8_stg)AND(@addressLine9=@addressLine9_stg))
						BEGIN
							SET @MsgTxt = @MsgTxt + 'Addr upd8, '
							--Select address id to mark as deleted
							SELECT 
								@ccustPremAddrIDBKP=AE_address,
								@ccustPremConIDBKP=AE_contact 
							FROM 
								BT_ENQUIRY_MASTER
							WITH (NOLOCK) 
							WHERE 
								AE_externalRef2=@pQMSRefNo
							--Tag address id to enquiry and estimate
							print 'Before upd '+convert(varchar,@ccustPremAddrID)
							UPDATE 
								BT_ENQUIRY_MASTER 
							SET 
								AE_address=@ccustPremAddrID
								,AE_CustomerAddress=@ccustPremAddrID
							WHERE 
								AE_externalRef2=@pQMSRefNo
									
							UPDATE 
								BT_ADDRESS_MASTER 
							SET 
								deleted='Y' 
							WHERE 
								ADDRESS_ID=@ccustPremAddrIDBKP
							--Update site name						
							UPDATE 
								BT_ADDRESS_MASTER 
							SET 
								sitename =(dbo.fn_rpt_GetReformatReturnAddress_NoSiteName(CAST(@ccustPremAddrID AS INT)))
							WHERE 
								ADDRESS_ID=@ccustPremAddrID;

							UPDATE 
								BT_ESTIMATE_MASTER 
							SET 
								AE_address=@ccustPremAddrID 
							WHERE 
								AE_RootID=@ENQAEID
							SET @correspondenceAddrId = @ccustPremAddrID 
						END--When address not matched
						ELSE -- When address not matched
						BEGIN
							SET @MsgTxt = @MsgTxt + 'Addr upd9, '
							UPDATE  
								BT_ADDRESS_MASTER
							SET  
								[addressLine1] = @addressLine1_stg,[addressLine2] = @addressLine2_stg
								,[addressLine3] = @addressLine3_stg,[addressLine4] = @addressLine4_stg
								,[addressLine5] = @addressLine5_stg,[addressLine6] = @addressLine6_stg
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
										/*FROM [BT_CUSTOMER_MASTER] cm
										INNER JOIN BT_ENQUIRY_MASTER enq
										ON enq.AE_customer = cm.cust_id
										INNER JOIN BT_ADDRESS_MASTER addr
										ON addr.parentID = cm.CUST_ID
										INNER JOIN BT_ADDRESS_MASTER_CRM_stg addrestg
										ON addrestg.QMS_REF = enq.AE_externalRef2
										WHERE enq.AE_externalRef2 = @pQMSRefNo*/
				 
										--MN: 26082021 SET CONCATINATED SITE NAME INTO SITE ADDRESS DETAILS
										--SET @vAEAddress =(SELECT TOP 1 AE_Address from BT_ENQUIRY_MASTER WHERE AE_externalRef2 = @pQMSRefNo)
							--Update site name
							UPDATE 
								BT_ADDRESS_MASTER 
							SET 
								sitename =dbo.fn_rpt_GetReformatReturnAddress_NoSiteName(@ae_address)
							WHERE 
								ADDRESS_ID=@ae_address;
													

										/*SELECT 
										@vAddressId= LTRIM(RTRIM(addr.ADDRESS_ID))
										,@vCust_ID=LTRIM(RTRIM(cm.CUST_ID))
										FROM [BT_CUSTOMER_MASTER] cm
										INNER JOIN BT_ENQUIRY_MASTER enq
										ON enq.AE_customer = cm.cust_id
										INNER JOIN BT_ADDRESS_MASTER addr
										ON addr.parentID = cm.CUST_ID
										INNER JOIN BT_ADDRESS_MASTER_CRM_stg addrestg
										ON addrestg.QMS_REF = enq.AE_externalRef2
										WHERE enq.AE_externalRef2 = @pQMSRefNo AND (addr.addressUsage=13 OR addr.addressUsage=1)*/
							--Update address id in customer master
							UPDATE 
								[BT_CUSTOMER_MASTER] 
							SET 
								CUST_PRIM_ADDRID=@ae_address 
							WHERE 
								CUST_ID=@vCUST
								END
						END
				END
				ELSE--When owner/agent customer not found
				BEGIN
					SET @MsgTxt = @MsgTxt + 'Addr upd10, '
					UPDATE  
						BT_ADDRESS_MASTER
					SET  
						[addressLine1] = @addressLine1_stg,[addressLine2] = @addressLine2_stg
						,[addressLine3] = @addressLine3_stg,[addressLine4] = @addressLine4_stg
						,[addressLine5] = @addressLine5_stg,[addressLine6] = @addressLine6_stg
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
						/*FROM [BT_CUSTOMER_MASTER] cm
						INNER JOIN BT_ENQUIRY_MASTER enq
						ON enq.AE_customer = cm.cust_id
						INNER JOIN BT_ADDRESS_MASTER addr
						ON addr.parentID = cm.CUST_ID
						INNER JOIN BT_ADDRESS_MASTER_CRM_stg addrestg
						ON addrestg.QMS_REF = enq.AE_externalRef2
						WHERE enq.AE_externalRef2 = @pQMSRefNo*/
				 
						--MN: 26082021 SET CONCATINATED SITE NAME INTO SITE ADDRESS DETAILS
						--SET @vAEAddress =(SELECT TOP 1 AE_Address from BT_ENQUIRY_MASTER WHERE AE_externalRef2 = @pQMSRefNo)
						--Update site name
						UPDATE 
							BT_ADDRESS_MASTER 
						SET 
							sitename =dbo.fn_rpt_GetReformatReturnAddress_NoSiteName(@ae_address)
						WHERE 
							ADDRESS_ID=@ae_address;
												

						/*SELECT 
						@vAddressId= LTRIM(RTRIM(addr.ADDRESS_ID))
						,@vCust_ID=LTRIM(RTRIM(cm.CUST_ID))

						FROM [BT_CUSTOMER_MASTER] cm
						INNER JOIN BT_ENQUIRY_MASTER enq
						ON enq.AE_customer = cm.cust_id
						INNER JOIN BT_ADDRESS_MASTER addr
						ON addr.parentID = cm.CUST_ID
						INNER JOIN BT_ADDRESS_MASTER_CRM_stg addrestg
						ON addrestg.QMS_REF = enq.AE_externalRef2
						WHERE enq.AE_externalRef2 = @pQMSRefNo AND (addr.addressUsage=13 OR addr.addressUsage=1)
						*/
						--Update address id in customer master
						UPDATE 
							[BT_CUSTOMER_MASTER] 
						SET 
							CUST_PRIM_ADDRID=@ae_address 
						WHERE 
							CUST_ID=@vCUST
				END
				SET @MsgTxt = @MsgTxt + 'End address '+CONVERT( VARCHAR(24), GETDATE(), 121)+', '
				--Update Contact
				IF(@vOwnerOrAgentOccupier='Agent') --For agent
				BEGIN
					SET @MsgTxt = @MsgTxt + 'Cont upd1, '
					IF(@ccustPremConID>0) -- If contact id exists for existing customer
					BEGIN					
						SET @MsgTxt = @MsgTxt + 'Cont upd2, '
						--Compare contact details with stg and master table		
						IF((@con_forename = @con_forename_stg)AND(@con_surname=@con_surname_stg)
						AND(@con_telephone=@con_telephone_stg)AND(@con_mobile=@con_mobile_stg)
						AND(@con_email=@con_email_stg))
						BEGIN
							SET @MsgTxt = @MsgTxt + 'Cont upd3, '
							--Select contact id which was created in initial load, to mark as deleted
							SELECT 
								@ccustPremConIDBKP=AE_contact 
							FROM 
								BT_ENQUIRY_MASTER 
							WITH (NOLOCK)
							WHERE 
								AE_externalRef2=@pQMSRefNo
							--Tag the contact id to enquiry					
							UPDATE 
								BT_ENQUIRY_MASTER 
							SET 
								AE_contact=CAST(@ccustPremConID AS varchar(50))
							WHERE 
								AE_externalRef2=@pQMSRefNo
							--Mark contact as deleted which was inserted in initial load					
							UPDATE 
								BT_CONTACTS_MASTER 
							SET 
								con_deleted='Y' 
							WHERE 
								con_contactID=@ccustPremConIDBKP
							--Tag contact id to estimate
							UPDATE 
								BT_ESTIMATE_MASTER 
							SET 
								AE_contact=CAST(@ccustPremConID AS varchar(50)) 
							WHERE 
								AE_RootID=@ENQAEID
												
						END
						ELSE
						BEGIN
							SET @MsgTxt = @MsgTxt + 'Cont upd4, '
							IF  @vdomesticorCommercial = 'domestic'
							BEGIN
								SET @MsgTxt = @MsgTxt + 'Cont upd5, '
								UPDATE  
									BT_CONTACTS_MASTER
								SET 
									[con_surname] = @con_surname_stg,
									[con_forename] = @con_forename_stg
									,[con_personalTitle] = @con_personalTitle_stg 
									,[con_telephone] = @con_telephone_stg
									,[con_mobile] = @con_mobile_stg,
									[con_email] = @con_email_stg
									,con_lastUpdated = GETDATE()
									,con_lastUpdatedBy = @ploggedonUser,
									con_contactName=CONCAT(@con_personalTitle_stg,' ',@con_forename_stg,' ',@con_surname_stg),
									con_parentid = @ccustid
								WHERE
									con_contactID = @contactID
								/*FROM [BT_CUSTOMER_MASTER] cm
								INNER JOIN BT_ENQUIRY_MASTER enq
								ON enq.AE_customer = cm.cust_id
								INNER JOIN BT_CONTACTS_MASTER con
								ON con.con_parentID = cm.CUST_ID
								INNER JOIN BT_CONTACT_MASTER_CRM_STG constg
								ON constg.QMS_REF = enq.AE_externalRef2
								WHERE enq.AE_externalRef2 = @pQMSRefNo*/
													
							END
							ELSE
							BEGIN
								SET @MsgTxt = @MsgTxt + 'Cont upd6, '
								UPDATE  
									BT_CONTACTS_MASTER
								SET								
									[con_surname] = @con_surname_stg
									,[con_forename] = @con_forename_stg
									,[con_personalTitle] = '' ,[con_telephone] = @con_telephone_stg
									,[con_mobile] = @con_mobile_stg,[con_email] = @con_email_stg
									,[con_lastUpdated] = GETDATE()
									,[con_lastUpdatedBy] = @ploggedonUser,
									[con_contactName]=CONCAT(@con_personalTitle_stg,' ',@con_forename_stg,' ',@con_surname_stg),
									con_parentid = @ccustid
								WHERE
									con_contactID = @contactID
								/*FROM 
									[BT_CUSTOMER_MASTER] cm
								INNER JOIN BT_ENQUIRY_MASTER enq
								ON enq.AE_customer = cm.cust_id
								INNER JOIN BT_CUSTOMER_MASTER_CRM_stg custg
								ON custg.QMS_REF = enq.AE_externalRef2
								INNER JOIN BT_CONTACTS_MASTER con
								ON con.con_parentID = cm.CUST_ID
								INNER JOIN BT_CONTACT_MASTER_CRM_STG constg
								ON constg.QMS_REF = enq.AE_externalRef2
								WHERE enq.AE_externalRef2 = @pQMSRefNo*/
														
														
							END

						END
					END
					ELSE
					BEGIN
						SET @MsgTxt = @MsgTxt + 'Cont upd7, '
					/*Update Contact*/
						IF  @vdomesticorCommercial = 'domestic'
						BEGIN
							SET @MsgTxt = @MsgTxt + 'Cont upd8, '				
							UPDATE  
								BT_CONTACTS_MASTER
							SET 
								[con_surname] = @con_surname_stg,
								[con_forename] = @con_forename_stg
								,[con_personalTitle] = @con_personalTitle_stg 
								,[con_telephone] = @con_telephone_stg
								,[con_mobile] = @con_mobile_stg,
								[con_email] = @con_email_stg
								,con_lastUpdated = GETDATE()
								,con_lastUpdatedBy = @ploggedonUser,
								con_contactName=CONCAT(@con_personalTitle_stg,' ',@con_forename_stg,' ',@con_surname_stg),
								con_parentid = @ccustid
							WHERE
								con_contactID = @contactID
							/*FROM [BT_CUSTOMER_MASTER] cm
							INNER JOIN BT_ENQUIRY_MASTER enq
							ON enq.AE_customer = cm.cust_id
							INNER JOIN BT_CONTACTS_MASTER con
							ON con.con_parentID = cm.CUST_ID
							INNER JOIN BT_CONTACT_MASTER_CRM_STG constg
							ON constg.QMS_REF = enq.AE_externalRef2
							WHERE enq.AE_externalRef2 = @pQMSRefNo*/
													
						END
						ELSE
						BEGIN
							SET @MsgTxt = @MsgTxt + 'Cont upd9, '
							
							UPDATE  
								BT_CONTACTS_MASTER
							SET 
								[con_surname] = @con_surname_stg
								,[con_forename] = @con_forename_stg
								,[con_personalTitle] = '' ,[con_telephone] = @con_telephone_stg
								,[con_mobile] = @con_mobile_stg,[con_email] = @con_email_stg
								,[con_lastUpdated] = GETDATE()
								,[con_lastUpdatedBy] = @ploggedonUser,
								[con_contactName]=CONCAT(@con_personalTitle_stg,' ',@con_forename_stg,' ',@con_surname_stg),
								con_parentid = @ccustid
							WHERE
								con_contactID = @contactID
							/*FROM [BT_CUSTOMER_MASTER] cm
							INNER JOIN BT_ENQUIRY_MASTER enq
							ON enq.AE_customer = cm.cust_id
							INNER JOIN BT_CUSTOMER_MASTER_CRM_stg custg
							ON custg.QMS_REF = enq.AE_externalRef2
							INNER JOIN BT_CONTACTS_MASTER con
							ON con.con_parentID = cm.CUST_ID
							INNER JOIN BT_CONTACT_MASTER_CRM_STG constg
							ON constg.QMS_REF = enq.AE_externalRef2
							WHERE enq.AE_externalRef2 = @pQMSRefNo*/
						END	 
					END


				END

				ELSE IF(@cnt>0)--For owner
				BEGIN
					SET @MsgTxt = @MsgTxt + 'Cont upd10, '
					IF(@ccustPremConID>0)
					BEGIN
						SET @MsgTxt = @MsgTxt + 'Cont upd11, '
						print 'Contstg'+@con_forename_stg
						print 'Cont'+@con_forename
						--Compare existing contact with stg contact
						IF((@con_forename = @con_forename_stg)AND(@con_surname=@con_surname_stg)
						AND(@con_telephone=@con_telephone_stg)AND(@con_mobile=@con_mobile_stg)
						AND(@con_email=@con_email_stg))
						BEGIN
							SET @MsgTxt = @MsgTxt + 'Cont upd12, '
							--Select contact id to mark as deleted which was created in initial load
							SELECT 
								@ccustPremConIDBKP=AE_contact 
							FROM 
								BT_ENQUIRY_MASTER 
							WITH (NOLOCK)
							WHERE 
								AE_externalRef2=@pQMSRefNo
							--Tag the contact to enq and estimate					
							UPDATE 
								BT_ENQUIRY_MASTER 
							SET 
								AE_contact=CAST(@ccustPremConID AS varchar(50))
							WHERE 
								AE_externalRef2=@pQMSRefNo
												
							UPDATE 
								BT_CONTACTS_MASTER 
							SET 
								con_deleted='Y' 
							WHERE 
								con_contactID=@ccustPremConIDBKP

							UPDATE 
								BT_ESTIMATE_MASTER 
							SET 
								AE_contact=CAST(@ccustPremConID AS varchar(50)) 
							WHERE 
								AE_RootID=@ENQAEID
												
						END
						ELSE
						BEGIN
							SET @MsgTxt = @MsgTxt + 'Cont upd13, '
							IF  @vdomesticorCommercial = 'domestic'
							BEGIN
								SET @MsgTxt = @MsgTxt + 'Cont upd14, '						
								UPDATE  
									BT_CONTACTS_MASTER
								SET 
									[con_surname] = @con_surname_stg,
									[con_forename] = @con_forename_stg
									,[con_personalTitle] = @con_personalTitle_stg 
									,[con_telephone] = @con_telephone_stg
									,[con_mobile] = @con_mobile_stg,
									[con_email] = @con_email_stg
									,con_lastUpdated = GETDATE()
									,con_lastUpdatedBy = @ploggedonUser,
									con_contactName=CONCAT(@con_personalTitle_stg,' ',@con_forename_stg,' ',@con_surname_stg),
									con_parentid = @ccustid
							WHERE
								con_contactID = @contactID
								/*FROM [BT_CUSTOMER_MASTER] cm
								INNER JOIN BT_ENQUIRY_MASTER enq
								ON enq.AE_customer = cm.cust_id
								INNER JOIN BT_CONTACTS_MASTER con
								ON con.con_parentID = cm.CUST_ID
								INNER JOIN BT_CONTACT_MASTER_CRM_STG constg
								ON constg.QMS_REF = enq.AE_externalRef2
								WHERE enq.AE_externalRef2 = @pQMSRefNo*/
																
							END
							ELSE
							BEGIN
								SET @MsgTxt = @MsgTxt + 'Cont upd15, '
								UPDATE  
									BT_CONTACTS_MASTER
								SET 
									[con_surname] = @con_surname_stg,
									[con_forename] = @con_forename_stg
									,[con_personalTitle] = '' 
									,[con_telephone] = @con_telephone_stg
									,[con_mobile] = @con_mobile_stg,
									[con_email] = @con_email_stg
									,con_lastUpdated = GETDATE()
									,con_lastUpdatedBy = @ploggedonUser,
									con_contactName=CONCAT(@con_personalTitle_stg,' ',@con_forename_stg,' ',@con_surname_stg),
									con_parentid = @ccustid
								WHERE
									con_contactID = @contactID
								/*FROM [BT_CUSTOMER_MASTER] cm
								INNER JOIN BT_ENQUIRY_MASTER enq
								ON enq.AE_customer = cm.cust_id
								INNER JOIN BT_CUSTOMER_MASTER_CRM_stg custg
								ON custg.QMS_REF = enq.AE_externalRef2
								INNER JOIN BT_CONTACTS_MASTER con
								ON con.con_parentID = cm.CUST_ID
								INNER JOIN BT_CONTACT_MASTER_CRM_STG constg
								ON constg.QMS_REF = enq.AE_externalRef2
								WHERE enq.AE_externalRef2 = @pQMSRefNo*/
																	
							END	 
						END	
					END
				END
				ELSE
				BEGIN
					SET @MsgTxt = @MsgTxt + 'Cont upd16, '
					IF  @vdomesticorCommercial = 'domestic'
					BEGIN
						SET @MsgTxt = @MsgTxt + 'Cont upd17, '
						UPDATE  
							BT_CONTACTS_MASTER
						SET 
							[con_surname] = @con_surname_stg,
							[con_forename] = @con_forename_stg
							,[con_personalTitle] = @con_personalTitle_stg 
							,[con_telephone] = @con_telephone_stg
							,[con_mobile] = @con_mobile_stg,
							[con_email] = @con_email_stg
							,con_lastUpdated = GETDATE()
							,con_lastUpdatedBy = @ploggedonUser,
							con_contactName=CONCAT(@con_personalTitle_stg,' ',@con_forename_stg,' ',@con_surname_stg),
							con_parentid = @ccustid
						WHERE
							con_contactID = @contactID
						/*FROM [BT_CUSTOMER_MASTER] cm
						INNER JOIN BT_ENQUIRY_MASTER enq
						ON enq.AE_customer = cm.cust_id
						INNER JOIN BT_CONTACTS_MASTER con
						ON con.con_parentID = cm.CUST_ID
						INNER JOIN BT_CONTACT_MASTER_CRM_STG constg
						ON constg.QMS_REF = enq.AE_externalRef2
						WHERE enq.AE_externalRef2 = @pQMSRefNo*/
					END
					ELSE
					BEGIN
						SET @MsgTxt = @MsgTxt + 'Cont upd18, '
						UPDATE  
							BT_CONTACTS_MASTER
						SET 
							[con_surname] = @vOwnerOnly_OrgName,[con_forename] = 'Commercial'
							,[con_personalTitle] = '' ,[con_telephone] = @con_telephone_stg
							,[con_mobile] = @con_mobile_stg,[con_email] = @con_email_stg
							,con_lastUpdated = GETDATE()
							,con_lastUpdatedBy = @ploggedonUser,
							con_contactName=CONCAT(@con_personalTitle_stg,' ',@con_forename_stg,' ',@con_surname_stg),
							con_parentid = @ccustid
						WHERE
							con_contactID = @contactID
						/*FROM [BT_CUSTOMER_MASTER] cm
						INNER JOIN BT_ENQUIRY_MASTER enq
						ON enq.AE_customer = cm.cust_id
						INNER JOIN BT_CUSTOMER_MASTER_CRM_stg custg
						ON custg.QMS_REF = enq.AE_externalRef2
						INNER JOIN BT_CONTACTS_MASTER con
						ON con.con_parentID = cm.CUST_ID
						INNER JOIN BT_CONTACT_MASTER_CRM_STG constg
						ON constg.QMS_REF = enq.AE_externalRef2
						WHERE enq.AE_externalRef2 = @pQMSRefNo*/
																	
					END	 
				END
				SET @MsgTxt = @MsgTxt + 'End contact '+CONVERT( VARCHAR(24), GETDATE(), 121)+', '
				--Update enquiry
				--Select CUST_PRIM_ADDRID
				SELECT		
					@vCustomerAddressId = CUST_PRIM_ADDRID
				FROM
					BT_CUSTOMER_MASTER
				WITH (NOLOCK)
				WHERE
					CUST_ID=@vCUST
					
				/*select  @vCustomerAddressId = cm.CUST_PRIM_ADDRID
				from BT_ENQUIRY_MASTER enq
				INNER JOIN BT_CUSTOMER_MASTER cm
				ON enq.AE_customer = cm.CUST_ID
				where enq.AE_internalRef = @vAE_internalRef*/
				--Select assigned to department
				SELECT  
					@vAE_assignedToDept = AE_assignedToDept,
					@ae_assignedToPerson = AE_assignedToPerson
				FROM 
					BT_ENQUIRY_MASTER_CRM_STG 
				WITH (NOLOCK)
				WHERE 
					QMS_REF = @pQMSRefNo

				
				/*SELECT 
					top 1  @vDepartment_Qpid_value = Department_Qpid_value
				FROM 
					DepartmentType_QPID_QMS_Map
				WITH (NOLOCK)
				WHERE 
					Department_QPID = @vAE_assignedToDept*/
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
				print 'assigntoperson id '+convert(varchar,@assignToPersonID)
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
				
				SET @MsgTxt = @MsgTxt + 'Enq upd, '
				UPDATE  
					enq
				SET 
					[AE_dateOrigin] = CAST(enqstg.AE_CompliantInfoReceiveDate AS SMALLDATETIME),
					[AE_orderReceivedDate] = CAST(enqstg.AE_dateOrigin AS SMALLDATETIME)
					,[AE_externalRef] = enqstg.AE_externalRef
					,[AE_summaryDescription] = enqstg.AE_summaryDescription
					--,AE_CustomerAddress = @vCustomerAddressId
					,AE_status = (select LOV_VALUE from MT_SYS_LOV
									where LOV_DESC='Small Works Live' 
									AND LOV_NAME='sysStatusEnquiry')
					,[AE_type] = (SELECT LOV_VALUE FROM MT_SYS_LOV 
								WHERE rtrim(ltrim(coalesce(LOV_Name,'')))='actEnquiryType' 
								AND LTRIM(RTRIM(UPPER(LOV_DESC))) = LTRIM(RTRIM(UPPER(enqstg.[Source]))) 
								AND LOV_DELETED=0)
				
					,[AE_CustType] = (SELECT TOP 1 CustomerType_QPID FROM customerType_QPID_QMS_Map 
										WHERE CustomerType_QMS= enqstg.AE_CustType)					
					,[AE_assignedToDept] = @vDepartment_Qpid_value
					,[AE_assignedToPerson] = @assignToPersonID
					,[AE_plotUnit] = enqstg.AE_plotUnit
					,[Source] = NULL
					,[AE_networkDistributor] =(SELECT TOP 1 LOV_VALUE FROM MT_SYS_LOV WHERE UPPER(rtrim(ltrim(coalesce(LOV_NAME,''))))='SYS_COMPANY' AND LTRIM(RTRIM(UPPER(LOV_DESC)))=RTRIM(LTRIM(UPPER(enqstg.AE_networkDistributor))) AND LOV_DELETED=0)
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
					AE_quoteDesigner = @QuoteDesignerId
					,[AE_WorkType] = (SELECT LOV_VALUE FROM MT_SYS_LOV WHERE rtrim(ltrim(coalesce(LOV_Name,'')))
								='sysWorkType' AND LTRIM(RTRIM(UPPER(LOV_DESC)))='SMALL WORKS' AND LOV_DELETED=0)
				FROM 
					BT_ENQUIRY_MASTER enq
				INNER JOIN
					BT_ENQUIRY_MASTER_CRM_stg enqstg
				ON 
					enqstg.QMS_REF = enq.AE_externalRef2
				WHERE
					enq.AE_AEID = @ENQAEID 
					AND enqstg.QMS_REF = @pQMSRefNo
					/*[BT_CUSTOMER_MASTER] cm
					INNER JOIN BT_ENQUIRY_MASTER enq
					ON enq.AE_customer = cm.cust_id
				INNER JOIN BT_ENQUIRY_MASTER_CRM_stg enqstg
				ON enqstg.QMS_REF = enq.AE_externalRef2
				WHERE enq.AE_externalRef2 = @pQMSRefNo*/

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

			

		/* End of Update---------------------------------------------------------------------------------------*/
		/* Begin Insert---------------------------------------------------------------------------------------*/
		
			/*Insert Site Address*/
			SELECT @vAddIdS = MAX(ADDRESS_ID)+1 FROM BT_ADDRESS_MASTER; 
			SET @MsgTxt = @MsgTxt + 'Insert site addr '+CONVERT(VARCHAR, @vAddIdS)+', '
			--Insert site address
			INSERT INTO 
				BT_ADDRESS_MASTER  
				([ADDRESS_ID],[parentID], [addressLine1],[addressLine2], [addressLine3]  
				,[addressLine4],[addressLine5] ,[addressLine6],[addressLine7] 
				,[addressLine8],[addressLine9]  ,[postCode],[easting] ,[Northing] ,
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
			
			--Update site address id in enquiry
			UPDATE 
				BT_enquiry_master 
			SET 
				AE_address=@vAddIdS 
			WHERE 
				AE_externalRef2=@pQMSRefNo
			
			--Update site name
			UPDATE 
				BT_ADDRESS_MASTER 
			SET 
				sitename =dbo.fn_rpt_GetReformatReturnAddress_NoSiteName(@vAddIdS) 
			WHERE 
				ADDRESS_ID=@vAddIdS;
			
			/*Agent Owner ocuupier*/
			/*SELECT 
				@vOwnerOccupiedAddrId = addr.ADDRESS_ID 
			FROM 
				BT_CUSTOMER_MASTER cm
			INNER JOIN 
				[dbo].BT_ENQUIRY_MASTER enq
			ON 
				enq.AE_customer = cm.CUST_ID
			INNER JOIN 
				BT_ADDRESS_MASTER addr
			ON 
				addr.parentID = cm.CUST_ID
			WHERE 
				enq.AE_AEID = @ENQAEID 
				AND (addr.addressUsage=1 OR addr.addressUsage=13)*/
			
			
			
			IF UPPER(LTRIM(RTRIM(@vOwnerOrAgentOccupier))) = 'OWNER'
			BEGIN
				
				SET @MsgTxt = @MsgTxt + 'Corr addr1 '+convert(varchar,@correspondenceAddrId)+', '
				--SET @vOwnerOccupiedAddrId = @correspondenceAddrId
				
					
				UPDATE 
					act
				SET 
					act.[AE_correspondenceAddress] = @correspondenceAddrId
				FROM 
					[dbo].BT_ENQUIRY_MASTER act
				WHERE 
					act.AE_AEID = @ENQAEID
			END
			
			ELSE
			BEGIN
				SET @MsgTxt = @MsgTxt + 'Corr addr2, '
				--Insert main address for agent
				DECLARE @vAddIDForOwner BIGINT;

				SELECT @vAddIDForOwner = MAX(ADDRESS_ID)+1 FROM BT_ADDRESS_MASTER; 
				SET @MsgTxt = @MsgTxt + 'Corr addr3 '+CONVERT(VARCHAR,@vAddIDForOwner)+', '
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

				--UPDATE BT_ADDRESS_MASTER SET 			
			
				
				DECLARE @EX_ID INT 
				Select @EX_ID=MAX(EX_ID)+1 from CDMS_ACT_AE_EXTRA 
		
				INSERT INTO 
					[dbo].[CDMS_ACT_AE_EXTRA]
					([EX_ID],[EX_AEID],[EX_clawBackAmount],[EX_dateCreated],[EX_dateCreatedBy],[EX_lastUpdated]
					,[EX_lastUpdatedBy], 
					[EX_deleted],[EX_PO_RefNumber],[EX_altContactAddress])
				VALUES
					(@EX_ID,@ENQAEID,0,GETDATE(),@ploggedonUser,GETDATE(),@ploggedonUser,'N',NULL
					,@correspondenceAddrId)
			END
			/*Agent Owner ocuupier end*/
			
			--Generate quote id for insert
			SELECT 
				@vQuoteId =  MAX(AE_AEID)+1 
			FROM 
				[BT_QUOTE_MASTER];
					DECLARE @OfferType Varchar(25)
				SELECT @OfferType=INT_LOV_ID FROM MT_SYS_LOV where LOV_NAME='SYSoffertype' and LOV_DESC='Standard';
			--Insert QUOTE 
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
				--,60  /*Changes By Madhumita*/
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
			/*INNER JOIN BT_ENQUIRY_MASTER enq
			ON enq.AE_externalRef2 = QUOSTG.QMS_REF*/
			INNER JOIN 
				BT_ENQUIRY_MASTER_CRM_stg enqstg
			ON 
				enqstg.QMS_REF = QUOSTG.QMS_REF				
			WHERE 
				enqstg.QMS_REF = @pQMSRefNo
				
			-- ANIRBAN DHARA
			SELECT @connectionnature =  ConnectionNature, @connectiontypechange = ConnectionTypeChange FROM [dbo].[BT_QUOTE_MASTER_CRM_STG] where QMS_REF=@pQMSRefNo


			--Insert BT_QUOTE_AE_EXTRA
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
				
				
			SELECT 
				@pCategory_addHist=AE_category,
				@ptype_addHist=AE_type,
				@pStatus_addHist=AE_status
			FROM 
				BT_QUOTE_MASTER
			WITH (NOLOCK) 
			WHERE 
				AE_AEID=@vQuoteId;
			
			print 'Calling sp sp_ACT_addHIST_addQuote'
			-- To be discuss add the staging table type,category,status
			EXEC sp_ACT_addHIST_addQuote @vQuoteId, @ploggedonUser, @pCategory_addHist, @ptype_addHist, '', '', 0, @pStatus_addHist

			

			SELECT 
				@networkdist=AE_networkDistributor 
			FROM 
				BT_ENQUIRY_MASTER 
			WITH (NOLOCK)
			WHERE 
				AE_AEID IN (SELECT AE_RootID FROM BT_QUOTE_MASTER 
							WHERE AE_internalRef='QUO'+ CONVERT(varchar(50), @vQuoteId))
	
			IF @networkdist=4000
			BEGIN
				SET @iAE_contractZone='zones1and2';
			END
			ELSE
			BEGIN
				SET @iAE_contractZone='zones3and4';
			END
			--Insert Estimate
			SELECT @newAEID=MAX(AE_AEID)+1 FROM BT_ESTIMATE_MASTER
			INSERT INTO 
				BT_ESTIMATE_MASTER
				(AE_AEID,AE_ParentID,AE_RootID,AE_customer,
				AE_type,AE_category,AE_status,AE_contractZone,AE_address,AE_metaModelType,AE_metaModelType2,
				AE_dateCreated,AE_createdBy,AE_lastUpdated,AE_lastUpdatedBy,AE_deleted,AE_sendToCFS,
				AE_isTemplate,AE_MPANRequested,AE_internalRef,AE_useUplift
				,AE_taskInstruction,AE_labourCost,AE_materialsCost,AE_contractCost,AE_companyID)
			VALUES
				(@newAEID,@vquoteID,@vEnqID,@ccustid,-1,'-1',1,@iAE_contractZone,/*@ae_address*/@vAddIdS,'T',17,getDate(),
				@ploggedonUser,getDate(),@ploggedonUser,'N','N','N','N',CONCAT('EST',@newAEID)
				,'Y','Y',0,0,0,1000);
				
			SET @MsgTxt = @MsgTxt + 'Begin BOM creation '+CONVERT( VARCHAR(24), GETDATE(), 121)+', '	
				/*Insert BOM*/
			/*Call Stored procedure to Add BOM*/
			
			DECLARE @vRate_Card_Top_ID INT;
			SELECT 
				@vBOMMaxCount=count(1) 
			FROM 
				BT_ACT_BILLOFMATERIALS_CRM_stg 
			WITH (NOLOCK)
			WHERE 
				BOM_Inserted = 'N'
				AND QMS_REF = @pQMSRefNo
			SET @MsgTxt = @MsgTxt + 'BOM Count '+CONVERT(VARCHAR,@vBOMMaxCount)+', '
			SET @vBOMMinCount =0
			WHILE @vBOMMinCount < @vBOMMaxCount
			BEGIN
				
				SELECT  
					TOP 1 @vprice_book_type_qms =  BOM_priceBook,@vDistributor_Type_QMS = PriceBookLocation,
					@vBOmType =  [BOM_type],@vBOM_ID = ID,@vRate_Card_Top_ID= Rate_Card_Top_ID,
					@vCustContPercentage =(CASE WHEN Isnumeric(BOM_customerContributionPercent)=1 
					Then Convert(float,BOM_customerContributionPercent) else 0 end),
					@vBOMDesc = BOM_description, @bom_InvestmentDriver = InvestmentDriver
				FROM 
					BT_ACT_BILLOFMATERIALS_CRM_stg 
				WITH (NOLOCK)
				WHERE 
					QMS_REF = @pQMSRefNo 
					AND BOM_Inserted = 'N'
			
				SET @MsgTxt = @MsgTxt + 'Pricebook '+ISNULL(@vprice_book_type_qms,'null')+', '

				IF @vBOmType = 'Customer'
					SET @vCustContPercentage =(CASE WHEN Isnumeric('100')=1 
					Then Convert(float,'100') else 0 end)	
				IF @vBOmType = 'NPG'
					SET @vCustContPercentage =(CASE WHEN Isnumeric('0')=1 
					Then Convert(float,'0') else 0 end)

				SELECT  
					@viBOM_priceBook = PriceBook_QPID_ID 
				FROM 
					PRICE_BOOK_Map 
				WITH (NOLOCK)
				WHERE 
					price_book_type_qms = @vprice_book_type_qms
					AND Distributor_Type_QMS = @vDistributor_Type_QMS

				SET @MsgTxt = @MsgTxt + 'Pricebook QPID ID'+ISNULL(@viBOM_priceBook,'null')+', '

				/*SELECT  
					@viBOM_priceBooktype = BOM_TYPE_QPID_ID 
				FROM 
					PRICE_BOOK_Map 
				WITH (NOLOCK)
				WHERE 
					BOM_TYPE_QMS = @vBOmType*/
				SELECT
					@viBOM_priceBooktype = LOV_VALUE
				FROM
					MT_SYS_LOV
				WITH (NOLOCK)
				WHERE
					rtrim(ltrim(coalesce(LOV_Name,'')))='sysBOMTaskType'
					AND LOV_DELETED=0  
					and (ISNULL(LOV_OBSOLETE,'N') <> 'Y' )
					AND LTRIM(RTRIM(UPPER(LOV_DESC))) = @vBOmType;
					
				--For Default value				
				SELECT
					@defaultBomTaskType = LOV_VALUE
				FROM
					MT_SYS_LOV
				WITH (NOLOCK)
				WHERE
					rtrim(ltrim(coalesce(LOV_Name,'')))='sysBOMTaskType'
					AND LOV_DELETED=0  
					and (ISNULL(LOV_OBSOLETE,'N') <> 'Y' )
					AND LTRIM(RTRIM(UPPER(LOV_DESC))) = 'Extension (sole use) assets';
					
			
				SET @viBOM_priceBooktype = ISNULL(UPPER(rtrim(ltrim(@viBOM_priceBooktype))),@defaultBomTaskType);
				SET @MsgTxt = @MsgTxt + 'BoM Task Type '+@viBOM_priceBooktype+', '
				/*SELECT 
					@vIGS_stdCode_4 = GS_stdCode_1 
				FROM 
					BT_ACT_STANDARDS_CRM_STG
				WHERE 
					QMS_REF = @pQMSRefNo
		
				SELECT  
					@vInvestmentDriver = InvestmentDriverValue 
				FROM 
					InvestmentDriver_QPID_QMS_Map 
				WHERE 
					CustomerType = @vdomesticorCommercial
					AND Standard = @vIGS_stdCode_4
				*/
				SET @MsgTxt = @MsgTxt + 'Inv driver recvd'+ISNULL(@bom_InvestmentDriver,'null')+', '
				SELECT  
					@vInvestmentDriver = LOV_VALUE 
				FROM 
					MT_SYS_LOV 
				WITH (NOLOCK)
				WHERE 
					LOV_DESC = @bom_InvestmentDriver
					AND LOV_NAME = 'sysInvestmentDriver'
					AND LOV_DELETED = 0;

				SET @MsgTxt = @MsgTxt + 'Inv found'+ISNULL(@vInvestmentDriver,'null')+', '
					
				SELECT @viBOM_estimateParentRef = CONCAT('EST',@newAEID)
				print 'Calling sp SP_ADD_BOM_QMS'
				exec SP_ADD_BOM_QMS 
				@iBOM_estimateParentRef=@viBOM_estimateParentRef,
				@iBOM_investmentDriver=@vInvestmentDriver,/*Change done by Madhumita*/
				@iBOM_name =@pQMSRefNo, 
				--@iBOM_type = '60' , 
				@iBOM_type = '1' , 
				@iBOM_priceBook =@viBOM_priceBook, 
				@iBOM_taskType =@viBOM_priceBooktype, 
				@iBOM_description =@vBOMDesc,
				@iBOM_status = '1', 
				@iBOM_addToProject =0, 
				@iBOM_dummyProject ='N', 
				@iBOM_customerContributionPercent = @vCustContPercentage,
				@iBOM_uplift  =0,--changes for uplift
				@userid =@ploggedonuser,
				@newBOMID =@vnewBOMID OUTPUT
			
				
				/*Insert Item task category*/
				SELECT 
					@vItemMaxCount = count(1) 
				FROM 
					BT_ACT_TASKCATEGORYITEMS_CRM_STG 
				WITH (NOLOCK)
				WHERE 
					QMS_REF = @pQMSRefNo 
					AND Rate_Card_Top_ID = @vRate_Card_Top_ID
					AND ItemInserted = 'N'
				
		
				SET @vItemMinCount =0
				WHILE @vItemMinCount < @vItemMaxCount
				BEGIN
					IF @vCustContPercentage = 100
					BEGIN
						SELECT 
							top 1 @vQty =   TCI_nonContestValQty,
							@vItemTaskCat = TCI_ID,--ItemDescription,
							@vItem_ID = ID
						FROM 
							BT_ACT_TASKCATEGORYITEMS_CRM_STG
						WITH (NOLOCK)
						WHERE 
							ItemInserted = 'N' AND QMS_REF = @pQMSRefNo 
							AND Rate_Card_Top_ID = @vRate_Card_Top_ID

						SELECT 
							@vTCI_Type = TCI_Type 
						FROM 
							[MT_PRICEBOOK] 
						WITH (NOLOCK)
						WHERE 
							--LTRIM(RTRIM(TCI_itemName)) = @vItemTaskCat
							LTRIM(RTRIM(TCI_CODE)) = @vItemTaskCat
							AND LTRIM(RTRIM(TCI_priceBook)) = LTRIM(RTRIM(@viBOM_priceBook))
						
						IF @vTCI_Type = 'money'
						BEGIN
							UPDATE  
								cat 
							SET 
								cat.TCI_nonContestValMoney = (CAST(ISNULL(@vQty,'-') AS INT)), 
								TCI_Type = @vTCI_Type
							FROM 
								BT_ACT_TASKCATEGORYITEMS cat
							INNER JOIN 
								BT_ACT_TASKCATEGORY item
							ON 
								cat.TCI_parentTCID = item.TC_TCID
							INNER JOIN 
								BT_ACT_BILLOFMATERIALS bom
							ON 
								item.TC_parentBOMID = bom.BOM_bomID
							WHERE 
								--cat.TCI_itemName = @vItemTaskCat
								cat.TCI_CODE = @vItemTaskCat
								AND bom.BOM_internalRef = @vnewBOMID
						END
						ELSE
						BEGIN
							UPDATE  
								cat 
							SET 
								cat.TCI_nonContestValQty = (CAST(ISNULL(@vQty,'-') AS INT)), 
								TCI_Type = @vTCI_Type
							FROM 
								BT_ACT_TASKCATEGORYITEMS cat
							INNER JOIN 
								BT_ACT_TASKCATEGORY item
							ON 
								cat.TCI_parentTCID = item.TC_TCID
							INNER JOIN 
								BT_ACT_BILLOFMATERIALS bom
							ON 
								item.TC_parentBOMID = bom.BOM_bomID
							WHERE 
								--cat.TCI_itemName = @vItemTaskCat
								cat.TCI_CODE = @vItemTaskCat
								AND bom.BOM_internalRef = @vnewBOMID
						END
						
					END
					ELSE IF @vCustContPercentage < 100
					BEGIN
						SELECT 
							top 1 @vQty =   TCI_ContestValQty, 
							@vItemTaskCat = TCI_ID,--ItemDescription,
							@vItem_ID = ID
						FROM 
							BT_ACT_TASKCATEGORYITEMS_CRM_STG
						WITH (NOLOCK)
						WHERE 
							ItemInserted = 'N' 
							AND QMS_REF = @pQMSRefNo 
							AND Rate_Card_Top_ID = @vRate_Card_Top_ID 

						SELECT 
							@vTCI_Type = TCI_Type 
						FROM 
							[MT_PRICEBOOK] 
						WITH (NOLOCK)
						WHERE 
							--LTRIM(RTRIM(TCI_itemName)) = @vItemTaskCat
							LTRIM(RTRIM(TCI_CODE)) = @vItemTaskCat
							AND LTRIM(RTRIM(TCI_priceBook)) = LTRIM(RTRIM(@viBOM_priceBook))

						/*UPDATE  
							cat 
						SET 
							cat.TCI_ContestValQty = (CAST(ISNULL(@vQty,'-') AS INT)), 
							TCI_Type = @vTCI_Type
						FROM 
							BT_ACT_TASKCATEGORYITEMS cat
						INNER JOIN 
							BT_ACT_TASKCATEGORY item
						ON 
							cat.TCI_parentTCID = item.TC_TCID
						INNER JOIN 
							BT_ACT_BILLOFMATERIALS bom
						ON 
							item.TC_parentBOMID = bom.BOM_bomID
						WHERE 
							--cat.TCI_itemName = @vItemTaskCat
							cat.TCI_CODE = @vItemTaskCat
							AND bom.BOM_internalRef = @vnewBOMID*/
						IF @vTCI_Type = 'money'
						BEGIN
							UPDATE  
								cat 
							SET 
								cat.TCI_contestValueMoney = (CAST(ISNULL(@vQty,'-') AS INT)), 
								TCI_Type = @vTCI_Type
							FROM 
								BT_ACT_TASKCATEGORYITEMS cat
							INNER JOIN 
								BT_ACT_TASKCATEGORY item
							ON 
								cat.TCI_parentTCID = item.TC_TCID
							INNER JOIN 
								BT_ACT_BILLOFMATERIALS bom
							ON 
								item.TC_parentBOMID = bom.BOM_bomID
							WHERE 
								--cat.TCI_itemName = @vItemTaskCat
								cat.TCI_CODE = @vItemTaskCat
								AND bom.BOM_internalRef = @vnewBOMID
						END
						ELSE
						BEGIN
							UPDATE  
								cat 
							SET 
								cat.TCI_ContestValQty = (CAST(ISNULL(@vQty,'-') AS INT)), 
								TCI_Type = @vTCI_Type
							FROM 
								BT_ACT_TASKCATEGORYITEMS cat
							INNER JOIN 
								BT_ACT_TASKCATEGORY item
							ON 
								cat.TCI_parentTCID = item.TC_TCID
							INNER JOIN 
								BT_ACT_BILLOFMATERIALS bom
							ON 
								item.TC_parentBOMID = bom.BOM_bomID
							WHERE 
								--cat.TCI_itemName = @vItemTaskCat
								cat.TCI_CODE = @vItemTaskCat
								AND bom.BOM_internalRef = @vnewBOMID
						END
						

					END
					UPDATE 
						BT_ACT_TASKCATEGORYITEMS_CRM_STG 
					SET 
						ItemInserted = 'Y' 
					WHERE 
						ID = @vItem_ID 
					
					SET @vItemMinCount = @vItemMinCount + 1
				END
				print 'Calling sp SP_ADD_BOM_TASKCAT_ITEM_COST'
				exec SP_ADD_BOM_TASKCAT_ITEM_COST 
					@vBOM_BOMREF=@vnewBOMID,
					@vuserid=@ploggedonuser
						--PRINT 'PROJ INSERT START'
				IF(@vBOMMinCount=0)
				BEGIN
								/*Insert Project*/
					SELECT @vTempNewBOMID=@vnewBOMID;
								 
					SELECT @vTempInvestmentDriver = @vInvestmentDriver;
								
					SELECT 
						@vEstimatedParentID= BOM_estimateParentID 
					FROM 
						BT_ACT_BILLOFMATERIALS
					WITH (NOLOCK) 
					WHERE 
						BOM_internalRef=@vTempNewBOMID

					IF @vDistributor_Type_QMS = 'North'
					BEGIN
						SET @vProjOrg = 'Call To Quote NE'
						SET @vTemplateType  = 'BN-01'
					END
					ELSE 
					BEGIN
						SET @vProjOrg = 'Call To Quote YE'
						SET @vTemplateType  = 'BY-01'
					END
					print 'Calling sp SP_CREATE_PROJECT_STRUCTURE'
					exec SP_CREATE_PROJECT_STRUCTURE 
						@vENQREF = @vAE_internalRef,
						@vBOMREF = @vnewBOMID,
						@TemplateID = '217', 
						@UID = @ploggedonUser, 
						@iBOM_fAE_Type = '500', 
						@iBOM_fAE_category  ='ZZZ',
						@iBOM_fAE_contract  ='ZZZ',
						@iBOM_fAE_projBillingOption = @vTemplateType,
						@iBOM_fAE_projOrg  =@vProjOrg,
						@fSUPP_customerType = '3',
						@fSUPP_providerOfEndConnection ='3'
						--,@connectionType = ''
								
								
				END
				ELSE
				BEGIN
					IF(@vInvestmentDriver<>@vTempInvestmentDriver)
					BEGIN
						IF @vDistributor_Type_QMS = 'North'
						BEGIN
							SET @vProjOrg = 'Call To Quote NE'
							SET @vTemplateType  = 'BN-01'
						END
						ELSE 
						BEGIN
							SET @vProjOrg = 'Call To Quote YE'
							SET @vTemplateType  = 'BY-01'
						END
						print 'Calling sp SP_CREATE_PROJECT_STRUCTURE'
						exec SP_CREATE_PROJECT_STRUCTURE 
							@vENQREF = @vAE_internalRef,
							@vBOMREF = @vnewBOMID,
							@TemplateID = '217', 
							@UID = @ploggedonUser, 
							@iBOM_fAE_Type = '500', 
							@iBOM_fAE_category  ='ZZZ',
							@iBOM_fAE_contract  ='ZZZ',
							@iBOM_fAE_projBillingOption = @vTemplateType,
							@iBOM_fAE_projOrg  =@vProjOrg,
							@fSUPP_customerType = '3',
							@fSUPP_providerOfEndConnection ='3'
							--,@connectionType = ''

										
					END
								
				END
				/*Insert Project End*/				
				
				UPDATE 
					BT_ACT_BILLOFMATERIALS_CRM_stg 
				SET 
					BOM_Inserted = 'Y' 
				WHERE 
					ID = @vBOM_ID

				SET @vBOMMinCount = @vBOMMinCount + 1
			END		
			print 'Before link bom to project'
			EXEC usp_QMS_LINK_BOM_TO_PROJECT
					@pEstimatedParentID=@vEstimatedParentID,
					@pInvestmentDriver=@vTempInvestmentDriver

			--Add BOM to project
				SELECT 
					@vBOM_generatedProject=BOM_generatedProject 
				FROM 
					BT_ACT_BILLOFMATERIALS 
				WHERE 
					BOM_estimateParentID=@vEstimatedParentID
					AND ISNULL(BOM_generatedProject,0)>0;
				
				UPDATE 
					BT_ACT_BILLOFMATERIALS  
				SET    
					BOM_dummyProject = 'N',   
					BOM_addToProject = @vBOM_generatedProject,		 
					BOM_lastUpdatedBy = @ploggedonUser,   
					BOM_lastUpdated = getDate()  

				WHERE 
					BOM_estimateParentID=@vEstimatedParentID
					AND ISNULL(BOM_generatedProject,0)=0;				

			print 'After link bom to project'


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
			   --2
			/*FROM BT_ACT_STANDARDS_CRM_STG AS Stdstg
				INNER JOIN BT_ENQUIRY_MASTER enq
				ON enq.AE_externalRef2 = Stdstg.QMS_REF
				INNER JOIN BT_ENQUIRY_MASTER_CRM_stg enqstg
				ON enqstg.QMS_REF = enq.AE_externalRef2
				WHERE enq.AE_externalRef2 = @pQMSRefNo*/
				
			--Update assigned to person and assigned to department in project
			UPDATE
				BT_PROJ_U_HEADER
			SET
				AE_assignedToDept = @vDepartment_Qpid_value,
				AE_assignedToPerson = @assignToPersonID
			WHERE
				AE_RootID=@ENQAEID;
				
		END -- End region
			
			--Update status in BT_Message 
			UPDATE 
				BT_Message 
			SET 
				Message_Status = 'P', 
				Error_Description='', 
				Customer_Name=@custName 
			WHERE 
			--QMS_Reference_Number = @pQMSRefNo
			ID=@btMsgID
			
		COMMIT TRAN
			--For logging
			SET @MsgTxt = @MsgTxt + 'END '+ CONVERT( VARCHAR(24), GETDATE(), 121);
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
		UPDATE BT_Message SET Error_Description=LEFT(@pErrorMessage, 500),Message_Status='F' WHERE ID=@btMsgID AND Message_Status = 'I'--QMS_Reference_Number = @pQMSRefNo
		--BF01: Error_Description is varchar(500); a message already committed as 'P' is never overwritten
		--For logging
		SET @MsgTxt = @MsgTxt + 'END WITH ERROR '+ CONVERT( VARCHAR(24), GETDATE(), 121);
		exec usp_CreateEntryForExecutionTime @pQMSRefNo,@MsgTxt;
	END CATCH
END
GO
