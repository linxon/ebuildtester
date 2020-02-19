import gentoopm

class AtomException(Exception):
    pass

class Atom(object):

    def __init__(self, atom):
        # We expect an atom of the form [=]CATEGORY/PACKAGE[-VERSION].
        self.atom = atom
        self.category = None
        self.package = None
        self.version = None

        try:
            pm = gentoopm.get_package_manager()

            self.category = pm.Atom(self.atom).key.category
            self.package = pm.Atom(self.atom).key.package
            self.version = pm.Atom(self.atom).version

            if self.category is None or self.package is None:
                raise ValueError
        except ValueError:
            raise AtomException("ATOM has to be of the form [=]SECTION/PACKAGE[-VERSION]")

    def __str__(self):
        if self.version is not None:
            prefix = "="
            suffix = "-" + self.version
        else:
            prefix = ""
            suffix = ""
        return prefix + self.category + "/" + self.package + suffix

    def __eq__(self, other):
        result = (self.atom == other.atom)
        return result

    def __repr__(self):
        return "Atom(\"%s\")" % self.__str__()
