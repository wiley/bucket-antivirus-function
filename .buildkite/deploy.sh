# need built package
buildkite-agent artifact download build/lambda.zip .

aws s3 cp build/lambda.zip s3://$1-s3-antivirus-lambda-code/bucket-antivirus-function-$2.zip
