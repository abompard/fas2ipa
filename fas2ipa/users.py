import string
from collections import defaultdict

import click
import progressbar
import python_freeipa

from .status import Status, print_status
from .utils import ObjectManager
from .statistics import Stats


class Users(ObjectManager):
    def __init__(self, *args, agreements, **kwargs):
        super().__init__(*args, **kwargs)
        self.agreements = agreements

    def migrate_users(self, users_start_at=None):
        alphabet = list(string.ascii_lowercase)
        if users_start_at:
            start_index = alphabet.index(users_start_at.lower())
            del alphabet[:start_index]

        stats = Stats()

        for letter in alphabet:
            click.echo(f"finding users starting with {letter}")
            users = self.fas.send_request(
                "/user/list",
                req_params={"search": letter + "*"},
                auth=True,
                timeout=240,
            )
            users_stats = self._migrate_users(users["people"])
            stats.update(users_stats)

        return stats

    def _migrate_users(self, users):
        print(f"{len(users)} found")
        if not users:
            return

        users.sort(key=lambda u: u["username"])

        counter = 0
        added = 0
        edited = 0
        groups_to_member_usernames = defaultdict(list)
        groups_to_sponsor_usernames = defaultdict(list)
        agreements_to_usernames = defaultdict(list)
        max_length = max([len(u["username"]) for u in users])

        for person in progressbar.progressbar(users, redirect_stdout=True):
            counter += 1
            self.check_reauth(counter)
            click.echo(person["username"].ljust(max_length + 2), nl=False)
            # Add user
            status = self.migrate_user(person)
            # Record membership
            for groupname, membership in person["group_roles"].items():
                if groupname in self.config["ignore_groups"]:
                    continue
                groups_to_member_usernames[groupname].append(person["username"])
                if membership["role_type"] in ["administrator", "sponsor"]:
                    groups_to_sponsor_usernames[groupname].append(person["username"])
            # Record agreement signatures
            group_names = [g["name"] for g in person["memberships"]]
            for agreement in self.config.get("agreement"):
                if set(agreement["signed_groups"]) & set(group_names):
                    # intersection is not empty: the user signed it
                    agreements_to_usernames[agreement["name"]].append(
                        person["username"]
                    )
            # Status
            print_status(status)
            if status == Status.ADDED:
                added += 1
            elif status == Status.UPDATED:
                edited += 1

        self.agreements.record_user_signatures(agreements_to_usernames)
        self.add_users_to_groups(groups_to_member_usernames, "members")
        self.add_users_to_groups(groups_to_sponsor_usernames, "sponsors")
        return dict(user_counter=counter, users_added=added, users_edited=edited,)

    def migrate_user(self, person):
        if self.config["skip_user_add"]:
            return Status.SKIPPED
        if person["human_name"]:
            name = person["human_name"].strip()
            name_split = name.split(" ")
            if len(name_split) > 2 or len(name_split) == 1:
                first_name = "<fnu>"
                last_name = name
            else:
                first_name = name_split[0].strip()
                last_name = name_split[1].strip()
        else:
            name = "<fnu> <lnu>"
            first_name = "<fnu>"
            last_name = "<lnu>"
        try:
            user_args = dict(
                first_name=first_name,
                last_name=last_name,
                full_name=name,
                gecos=name,
                display_name=name,
                home_directory="/home/fedora/%s" % person["username"],
                disabled=person["status"] != "active",
                # If they haven't synced yet, they must reset their password:
                random_pass=True,
                fasircnick=person["ircnick"].strip() if person["ircnick"] else None,
                faslocale=person["locale"].strip() if person["locale"] else None,
                fastimezone=person["timezone"].strip() if person["timezone"] else None,
                fasgpgkeyid=[person["gpg_keyid"][:16].strip()]
                if person["gpg_keyid"]
                else None,
            )
            try:
                self.ipa.user_add(person["username"], **user_args)
                return Status.ADDED
            except python_freeipa.exceptions.FreeIPAError as e:
                if (
                    e.message
                    == 'user with name "%s" already exists' % person["username"]
                ):
                    # Update them instead
                    self.ipa.user_mod(person["username"], **user_args)
                    return Status.UPDATED
                else:
                    raise e

        except python_freeipa.exceptions.Unauthorized:
            self.ipa.login(
                self.config["ipa"]["username"], self.config["ipa"]["password"]
            )
            return self.migrate_user(person)
        except Exception as e:
            print(e)
            return Status.FAILED

    def add_users_to_groups(self, groups_to_users, category):
        if self.config["skip_user_membership"]:
            return

        if category not in ["members", "sponsors"]:
            raise ValueError("title must be eigher member or sponsor")

        click.echo(f"Adding {category} to groups")
        total = sum([len(members) for members in groups_to_users.values()])
        if total == 0:
            click.echo("Nothing to do.")
            return
        counter = 0
        with progressbar.ProgressBar(max_value=total, redirect_stdout=True) as bar:
            for group in sorted(groups_to_users):
                members = groups_to_users[group]
                for chunk in self.chunks(members):
                    counter += len(chunk)
                    self.check_reauth(counter)
                    try:
                        if category == "members":
                            self.ipa.group_add_member(group, chunk, no_members=True)
                        elif category == "sponsors":
                            self.ipa._request(
                                "group_add_member_manager", group, {"user": chunk}
                            )
                        print_status(
                            Status.ADDED,
                            f"Added {category} to {group}: {', '.join(chunk)}",
                        )
                    except python_freeipa.exceptions.ValidationError as e:
                        for msg in e.message["member"]["user"]:
                            if msg[1] != "This entry is already a member":
                                print_status(
                                    Status.FAILED,
                                    f"Failed to add {msg[0]} in the {category} of {group}: "
                                    + msg[1],
                                )
                    except python_freeipa.exceptions.NotFound as e:
                        print_status(
                            Status.FAILED,
                            f"Failed to add {chunk} in the {category} of {group}: {e}",
                        )
                    finally:
                        bar.update(counter)
