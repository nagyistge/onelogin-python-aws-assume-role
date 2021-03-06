#!/usr/bin/python

import base64
import boto3
import botocore
import getpass
import json
import os
import sys
import time

from lxml import etree as ET
from optparse import OptionParser
from optparse_mooi import CompactColorHelpFormatter

from onelogin.api.client import OneLoginClient

from writer import ConfigFileWriter


def get_options():
    help_formatter = CompactColorHelpFormatter(
        heading_color='white-bold',
        usage_color='yellow',
        shopt_color='yellow-bold',
        lopt_color='yellow-bold',
        metavar_color='white-bold',
        help_color='yellow',
        description_color='white',
        align_long_opts=True
    )
    parser = OptionParser(formatter=help_formatter)

    parser.add_option("-i", "--client_id", dest="client_id", type="string",
                      help="A valid OneLogin API client_id")
    parser.add_option("-s", "--client_secret", dest="client_secret", type="string",
                      help="A valid OneLogin API client_secret")
    parser.add_option("-r", "--region", dest="region", default="us", type="string",
                      help="us or eu  (Default value: us")

    parser.add_option("-t", "--time", dest="time", default=45, type="int",
                      help="Sleep time between iterations, in minutes  [15-60 min]")
    parser.add_option("-l", "--loop", dest="loop", default=1, type="int",
                      help="Number of iterations")
    parser.add_option("-p", "--profile", dest="profile_name", type="string",
                      help="Save Temporal AWS credentials using that profile name")
    parser.add_option("-f", "--file", dest="file", type="string",
                      help="Set a custom path to save the AWS credentials. (if not used, default AWS path is used)")
    (options, args) = parser.parse_args()

    options.time = options.time
    if options.time < 15:
        options.time = 15
    elif options.time > 60:
        options.time = 60

    return options


def get_client(options):
    client_id = client_secret = None

    if options.client_id is not None and options.client_secret is not None:
        client_id = options.client_id
        client_secret = options.client_secret
        region = options.region
    else:
        if os.path.isfile('onelogin.sdk.json'):
            json_data = open('onelogin.sdk.json').read()
            data = json.loads(json_data)
            if 'client_id' in data.keys() and 'client_secret' in data.keys():
                client_id = data['client_id']
                client_secret = data['client_secret']
                region = data.get('region', 'us')

    if client_id is None or client_secret is None:
        raise Exception("OneLogin Client ID and Secret are required")

    return OneLoginClient(client_id, client_secret, region)


def check_device_exists(devices, device_id):
    for device in devices:
        if device.id == device_id:
            return True
    return False


def get_saml_response(client, username_or_email, password, app_id, onelogin_subdomain, mfa_verify_info=None):
    saml_endpoint_response = client.get_saml_assertion(username_or_email, password, app_id, onelogin_subdomain)

    try_get_saml_response = 0
    while saml_endpoint_response is None or saml_endpoint_response.type == "pending":
        time.sleep(30)
        saml_endpoint_response = client.get_saml_assertion(username_or_email, password, app_id, onelogin_subdomain)
        try_get_saml_response += 1
        if try_get_saml_response == 10:
            sys.exit()

    if saml_endpoint_response.type == "success":
        if saml_endpoint_response.mfa is not None:
            mfa = saml_endpoint_response.mfa
            devices = mfa.devices

            if mfa_verify_info is None:
                print("\nMFA Required")
                print("Authenticate using one of these devices:")
            else:
                device_id = mfa_verify_info['device_id']
                if not check_device_exists(devices, device_id):
                    print("\nThe device selected with ID %s is not available anymore" % device_id)
                    print("Those are the devices available now:")
                    mfa_verify_info = None

            if mfa_verify_info is None:
                print("-----------------------------------------------------------------------")
                for index, device in enumerate(devices):
                    print(" " + str(index) + " | " + device.type)

                print("-----------------------------------------------------------------------")
                print("\nSelect the desired MFA Device [0-%s]: " % (len(devices) - 1))
                device_selection = int(sys.stdin.readline().strip())
                device = devices[device_selection]
                device_id = device.id

                print("Enter the OTP Token for %s: " % device.type)
                otp_token = sys.stdin.readline().strip()
                state_token = mfa.state_token
                mfa_verify_info = {
                    'otp_token': otp_token,
                    'state_token': state_token
                }
            else:
                otp_token = mfa_verify_info['otp_token']
                state_token = mfa_verify_info['state_token']

            saml_endpoint_response = client.get_saml_assertion_verifying(app_id, device_id, state_token, otp_token)
            while client.error_description == "Failed authentication with this factor":
                print("The OTP Token was invalid, please introduce a new one: ")
                otp_token = sys.stdin.readline().strip()
                saml_endpoint_response = client.get_saml_assertion_verifying(app_id, device_id, state_token, otp_token)
                mfa_verify_info['otp_token'] = otp_token

        saml_response = saml_endpoint_response.saml_response

    result = {
        'saml_response': saml_response,
        'mfa_verify_info': mfa_verify_info
    }
    return result


def get_attributes(saml_response):
    saml_response_xml = base64.b64decode(saml_response)
    saml_response_elem = ET.fromstring(saml_response_xml)
    NSMAP = {
        'samlp': 'urn:oasis:names:tc:SAML:2.0:protocol',
        'saml': 'urn:oasis:names:tc:SAML:2.0:assertion'
    }
    attributes = {}
    attribute_nodes = saml_response_elem.xpath('//saml:AttributeStatement/saml:Attribute', namespaces=NSMAP)
    for attribute_node in attribute_nodes:
        attr_name = attribute_node.get('Name')
        values = []
        for attr in attribute_node.iterchildren('{%s}AttributeValue' % NSMAP['saml']):
            values.append(attr.text)
        attributes[attr_name] = values
    return attributes


def main():
    print("\nOneLogin AWS Assume Role Tool\n")

    options = get_options()

    client = get_client(options)

    client.get_access_token()

    mfa_verify_info = None
    role_arn = principal_arn = None
    default_aws_region = 'us-west-2'

    config_file_writer = None
    botocore_config = botocore.client.Config(signature_version=botocore.UNSIGNED)

    for i in range(0, options.loop):
        if i == 0:
            # Capture OneLogin Account Details
            print("OneLogin Username: ")
            username_or_email = sys.stdin.readline().strip()

            password = getpass.getpass("\nOneLogin Password: ")

            print("\nAWS App ID: ")
            app_id = sys.stdin.readline().strip()

            print("\nOnelogin Instance Sub Domain: ")
            onelogin_subdomain = sys.stdin.readline().strip()
        else:
            time.sleep(options.time * 60)

        result = get_saml_response(client, username_or_email, password, app_id, onelogin_subdomain, mfa_verify_info)

        mfa_verify_info = result['mfa_verify_info']
        saml_response = result['saml_response']

        if i == 0:
            attributes = get_attributes(saml_response)
            if 'https://aws.amazon.com/SAML/Attributes/Role' not in attributes.keys():
                raise Exception("SAMLResponse from Identity Provider does not contain AWS Role info")
            else:
                roles = attributes['https://aws.amazon.com/SAML/Attributes/Role']
                selected_role = None
                if len(roles) > 1:
                    print("\nAvailable AWS Roles")
                    print("-----------------------------------------------------------------------")
                    for index, role in enumerate(roles):
                        role_info = role.split(":")
                        account_id = role_info[4]
                        role_name = role_info[5].replace("role/", "")
                        print(" %s | %s (Account %s)" % (index, role_name, account_id))
                    print("-----------------------------------------------------------------------")
                    print("Select the desired Role [0-%s]: " % (len(roles) - 1))
                    selected_role = roles[int(sys.stdin.readline().strip())]
                elif len(roles) == 1:
                    selected_role = roles[0]
                else:
                    print("SAMLResponse from Identity Provider does not contain available AWS Role for this user")

                if selected_role is not None:
                    selected_role_data = selected_role.split(',')
                    role_arn = selected_role_data[0]
                    principal_arn = selected_role_data[1]

        if i == 0:
            # AWS Region
            print("\nAWS Region (" + default_aws_region + "): ")
            aws_region = sys.stdin.readline().strip()
            if not aws_region or aws_region == "-":
                aws_region = default_aws_region

        conn = boto3.client('sts', region_name=aws_region, config=botocore_config)
        aws_session_token = conn.assume_role_with_saml(
            RoleArn=role_arn,
            PrincipalArn=principal_arn,
            SAMLAssertion=saml_response,
            DurationSeconds=3600
        )

        access_key_id = aws_session_token['Credentials']['AccessKeyId']
        secret_access_key = aws_session_token['Credentials']['SecretAccessKey']
        session_token = aws_session_token['Credentials']['SessionToken']
        arn = aws_session_token['AssumedRoleUser']['Arn']

        if options.profile_name is None and options.file is None:
            action = "export"
            if sys.platform.startswith('win'):
                action = "set"

            print("\n-----------------------------------------------------------------------\n")
            print("Success!\n")
            print("Assumed Role User: %s\n" % arn)
            print("Temporary AWS Credentials Granted via OneLogin\n")
            print("Copy/Paste to set these as environment variables\n")
            print("-----------------------------------------------------------------------\n")

            print("%s AWS_SESSION_TOKEN=%s\n" % (action, session_token))
            print("%s AWS_ACCESS_KEY_ID=%s\n" % (action, access_key_id))
            print("%s AWS_SECRET_ACCESS_KEY=%s\n" % (action, secret_access_key))
        else:
            if options.file is None:
                options.file = os.path.expanduser('~/.aws/credentials')

            if options.profile_name is None:
                options.profile_name = "default"

            if config_file_writer is None:
                config_file_writer = ConfigFileWriter()

            updated_config = {
                '__section__': options.profile_name,
                'aws_access_key_id': access_key_id,
                'aws_secret_access_key': secret_access_key,
                'aws_session_token': session_token,
            }
            config_file_writer.update_config(updated_config, options.file)

            print("Success!\n")
            print("Temporary AWS Credentials Granted via OneLogin\n")
            print("Updated AWS profile '%s' located at %s" % (options.profile_name, options.file))
            if options.loop > (i + 1):
                print("This process will regenerate credentials %s more times.\n" % (options.loop - (i + 1)))
                print("Press Ctrl + C to exit")

if __name__ == '__main__':
    main()
