class OpticType:

    def __init__(self, name, count):
        self.name = name
        self.count = count

    @staticmethod
    def make_optic_type(name):
        optic_type = OpticType(name, 1)
        return optic_type

    def add(self):
        self.count += 1

    def print_count(self):
        print(f"{self.name}: {self.count}")

    def compare_name(self, optic_type_input):
        if self.name.casefold() == optic_type_input.casefold():
            return True
        return False
    def string_count(self):
        return f"{self.name}: {self.count}"