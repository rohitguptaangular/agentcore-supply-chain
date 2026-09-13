# Supply Chain AI Assistant on Amazon Bedrock AgentCore
#
# Deployment uses plain CloudFormation, not the SAM CLI. `aws cloudformation
# package` uploads Lambda code and rewrites nested stack URLs, and the
# AWS::Serverless transform is expanded server-side, which is what
# CAPABILITY_AUTO_EXPAND grants. That keeps the toolchain to aws-cli alone.
#
# Normal first run:
#     make bootstrap package deploy seed frontend url

SHELL := /bin/bash
.SHELLFLAGS := -eu -o pipefail -c

# ---------------------------------------------------------------- settings
PROJECT        ?= supplychain
STACK          ?= $(PROJECT)
REGION         ?= us-east-1

# Cost flags. All three default to the cheap, working configuration.
ENABLE_VPC     ?= false
ENABLE_KB      ?= true

# s3         S3 Vectors. No hourly charge. ~100ms queries. The default.
# opensearch OpenSearch Serverless. Faster, but a classic collection bills a
#            2 OCU floor around the clock — roughly $350/month if left up.
VECTOR_STORE   ?= s3

ACCOUNT        := $(shell aws sts get-caller-identity --query Account --output text)
ARTIFACTS      := $(PROJECT)-artifacts-$(ACCOUNT)-$(REGION)
BUILD          := build

.PHONY: help bootstrap package deploy seed frontend outputs url logs destroy clean validate

help: ## Show available targets
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'

# ---------------------------------------------------------------- bootstrap
bootstrap: ## Create the S3 bucket holding agent zips, schemas and packaged templates
	@if aws s3api head-bucket --bucket $(ARTIFACTS) 2>/dev/null; then \
		echo "Artifacts bucket $(ARTIFACTS) already exists"; \
	else \
		echo "Creating $(ARTIFACTS)"; \
		if [ "$(REGION)" = "us-east-1" ]; then \
			aws s3api create-bucket --bucket $(ARTIFACTS) --region $(REGION); \
		else \
			aws s3api create-bucket --bucket $(ARTIFACTS) --region $(REGION) \
				--create-bucket-configuration LocationConstraint=$(REGION); \
		fi; \
		aws s3api put-public-access-block --bucket $(ARTIFACTS) \
			--public-access-block-configuration \
			BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true; \
	fi

# ------------------------------------------------------------------ package
package: bootstrap ## Zip the two agents and upload them with the gateway tool schemas
	@bash scripts/package_agents.sh $(ARTIFACTS) $(REGION)
	@echo "Uploading gateway tool schemas"
	@aws s3 cp schemas/ s3://$(ARTIFACTS)/schemas/ --recursive \
		--exclude "*" --include "*_tools.json" --region $(REGION)

# ------------------------------------------------------------------- deploy
validate: ## Check the root template parses
	@aws cloudformation validate-template \
		--template-body file://template.yaml --region $(REGION) >/dev/null
	@echo "template.yaml is valid"

deploy: ## Package and deploy the whole stack
	@mkdir -p $(BUILD)
	@echo "Packaging nested stacks and Lambda code"
	@aws cloudformation package \
		--template-file template.yaml \
		--s3-bucket $(ARTIFACTS) \
		--s3-prefix cloudformation \
		--output-template-file $(BUILD)/packaged.yaml \
		--region $(REGION)
	@echo "Deploying stack $(STACK)"
	@aws cloudformation deploy \
		--template-file $(BUILD)/packaged.yaml \
		--stack-name $(STACK) \
		--region $(REGION) \
		--capabilities CAPABILITY_IAM CAPABILITY_NAMED_IAM CAPABILITY_AUTO_EXPAND \
		--parameter-overrides \
			ProjectName=$(PROJECT) \
			ArtifactsBucket=$(ARTIFACTS) \
			EnableVpc=$(ENABLE_VPC) \
			EnableKnowledgeBase=$(ENABLE_KB) \
			VectorStore=$(VECTOR_STORE) \
		--no-fail-on-empty-changeset
	@$(MAKE) --no-print-directory outputs

# --------------------------------------------------------------------- seed
seed: ## Load dummy data into DynamoDB, upload documents and start ingestion
	@python3 scripts/seed.py --stack $(STACK) --region $(REGION)

# ----------------------------------------------------------------- frontend
frontend: ## Generate config.js from stack outputs and publish the UI
	@python3 scripts/publish_frontend.py --stack $(STACK) --region $(REGION) \
		--enable-vpc $(ENABLE_VPC) --enable-kb $(ENABLE_KB)

# ------------------------------------------------------------------ inspect
outputs: ## Print every stack output
	@aws cloudformation describe-stacks --stack-name $(STACK) --region $(REGION) \
		--query 'Stacks[0].Outputs[].[OutputKey,OutputValue]' --output table

url: ## Print the chat URL
	@aws cloudformation describe-stacks --stack-name $(STACK) --region $(REGION) \
		--query 'Stacks[0].Outputs[?OutputKey==`ChatUrl`].OutputValue' --output text

logs: ## Tail the chat handler logs
	@aws logs tail /aws/lambda/$(PROJECT)-chat-handler --follow --region $(REGION)

# ------------------------------------------------------------------ destroy
destroy: ## Delete everything. Buckets are emptied first or the stack will not delete.
	@echo "This deletes stack $(STACK) and all of its data."
	@read -p "Type the stack name to confirm: " confirm && [ "$$confirm" = "$(STACK)" ]
	@for bucket in $$(aws cloudformation describe-stacks --stack-name $(STACK) \
		--region $(REGION) --query \
		'Stacks[0].Outputs[?OutputKey==`FrontendBucket`||OutputKey==`KnowledgeBucket`].OutputValue' \
		--output text 2>/dev/null); do \
			echo "Emptying $$bucket"; \
			aws s3 rm s3://$$bucket --recursive --region $(REGION) >/dev/null || true; \
	done
	@aws cloudformation delete-stack --stack-name $(STACK) --region $(REGION)
	@echo "Delete requested. Waiting..."
	@aws cloudformation wait stack-delete-complete --stack-name $(STACK) --region $(REGION)
	@echo "Stack deleted."
	@echo "Kept on purpose: the artifacts bucket $(ARTIFACTS), and (on the s3 vector"
	@echo "store) the vector bucket $(PROJECT)-vectors-$(ACCOUNT) — a vector bucket"
	@echo "can only be deleted when empty, so it is retained rather than stalling the"
	@echo "stack delete. Remove both by hand when you are finished for good:"
	@echo "  aws s3vectors delete-index  --vector-bucket-name $(PROJECT)-vectors-$(ACCOUNT) --index-name $(PROJECT)-kb-index"
	@echo "  aws s3vectors delete-vector-bucket --vector-bucket-name $(PROJECT)-vectors-$(ACCOUNT)"

clean: ## Remove local build output
	@rm -rf $(BUILD) frontend/config.js
